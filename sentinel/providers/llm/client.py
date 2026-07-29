"""LLM access via LiteLLM (spec §2, §11).

- Models are resolved by ROLE from config/models.yaml — never hardcoded.
- Every call: JSON output validated against a pydantic schema, retry with
  backoff, token/cost logged to api_usage.
- Hard daily COST budget (policy.daily_cost_budget_usd in models.yaml,
  default $0.50) plus a token-count backstop: when either is exceeded,
  BudgetExceeded is raised before the call is made and the pipeline degrades
  automatically to deterministic-only signals (flagged as such). Degraded
  mode needs no operator action and has no override.
- LLM output NEVER becomes a numeric trade parameter.
"""

import json
import time
from datetime import UTC, datetime
from functools import lru_cache

import structlog
import yaml
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel.config import get_settings
from sentinel.db.models import ApiUsage
from sentinel.providers.credentials import get_credential
from sentinel.providers.types import ProviderCheck

log = structlog.get_logger()


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


@lru_cache
def _load_models_config() -> dict:
    path = get_settings().models_config_path
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _resolve_role(role: str) -> dict:
    cfg = _load_models_config()
    role_cfg = cfg.get("roles", {}).get(role)
    if not role_cfg:
        raise LLMError(f"role '{role}' not defined in models.yaml")
    active = role_cfg["active"]
    model_cfg = dict(role_cfg["candidates"][active])
    model_cfg["policy"] = cfg.get("policy", {})
    return model_cfg


def tokens_used_today(db: Session) -> int:
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = db.execute(
        select(func.sum(ApiUsage.tokens_in + ApiUsage.tokens_out)).where(
            ApiUsage.provider == "anthropic", ApiUsage.ts >= since
        )
    ).scalar_one_or_none()
    return int(total or 0)


def cost_used_today(db: Session) -> float:
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = db.execute(
        select(func.sum(ApiUsage.cost_usd)).where(
            ApiUsage.provider == "anthropic", ApiUsage.ts >= since
        )
    ).scalar_one_or_none()
    return float(total or 0.0)


def calls_made_today(db: Session) -> int:
    """Attempted calls today, successful or not — retries count.

    This is the backstop for the case the dollar meter can't catch: a model
    whose pricing LiteLLM cannot compute reports $0.00 forever, so without a
    count cap a runaway loop would never trip the budget.
    """
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = db.execute(
        select(func.count(ApiUsage.id)).where(
            ApiUsage.provider == "anthropic", ApiUsage.ts >= since
        )
    ).scalar_one_or_none()
    return int(total or 0)


def daily_cost_budget_usd() -> float:
    """Dollar cap from models.yaml policy; Settings value is the fallback."""
    policy = _load_models_config().get("policy") or {}
    try:
        return float(policy["daily_cost_budget_usd"])
    except (KeyError, TypeError, ValueError):
        return get_settings().llm_daily_cost_budget_usd


def daily_call_budget() -> int:
    """Hard call-count cap from models.yaml policy (0 = unlimited)."""
    policy = _load_models_config().get("policy") or {}
    try:
        return int(policy["max_llm_calls_per_day"])
    except (KeyError, TypeError, ValueError):
        return 0


def _check_budget(db: Session) -> None:
    """Raise BudgetExceeded when today's spend (or either backstop) is
    exhausted. Callers catch LLMError and fall back to deterministic output,
    so degraded mode activates automatically the moment the cap is hit."""
    cost_budget = daily_cost_budget_usd()
    cost_used = cost_used_today(db)
    if cost_used >= cost_budget:
        raise BudgetExceeded(
            f"daily cost budget exhausted (${cost_used:.4f}/${cost_budget:.2f})"
        )
    call_budget = daily_call_budget()
    if call_budget > 0:
        calls = calls_made_today(db)
        if calls >= call_budget:
            raise BudgetExceeded(f"daily call budget exhausted ({calls}/{call_budget})")
    token_budget = get_settings().llm_daily_token_budget
    used = tokens_used_today(db)
    if used >= token_budget:
        raise BudgetExceeded(f"daily token budget exhausted ({used}/{token_budget})")


def budget_status(db: Session) -> dict:
    """Today's LLM spend vs every cap — surfaced in the System view."""
    cost_budget = daily_cost_budget_usd()
    call_budget = daily_call_budget()
    token_budget = get_settings().llm_daily_token_budget
    cost = cost_used_today(db)
    calls = calls_made_today(db)
    tokens = tokens_used_today(db)
    return {
        "cost_usd": round(cost, 6),
        "cost_budget_usd": cost_budget,
        "calls": calls,
        "call_budget": call_budget,
        "tokens": tokens,
        "token_budget": token_budget,
        "degraded": (
            cost >= cost_budget
            or (call_budget > 0 and calls >= call_budget)
            or tokens >= token_budget
        ),
    }


def _record_usage(
    db: Session,
    endpoint: str,
    tokens_in: int,
    tokens_out: int,
    cost: float,
    ok: bool,
    detail: str = "",
) -> None:
    db.add(
        ApiUsage(
            provider="anthropic",
            endpoint=endpoint,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            ok=ok,
            detail=detail[:500],
        )
    )
    db.flush()


def complete_json[T: BaseModel](
    db: Session,
    role: str,
    system: str,
    user: str,
    schema: type[T],
    endpoint: str = "",
) -> T:
    """Call the role's model and validate the response against `schema`.

    Retries (with backoff) on transport errors AND on schema-invalid output —
    the validation error is fed back to the model on retry.
    """
    import litellm

    _check_budget(db)
    cfg = _resolve_role(role)
    policy = cfg.get("policy", {})
    max_retries = int(policy.get("max_retries", 3))
    backoff = float(policy.get("retry_backoff_base_seconds", 2))
    timeout = float(policy.get("request_timeout_seconds", 60))
    api_key = get_credential(db, "anthropic", "api_key") or None

    schema_json = json.dumps(schema.model_json_schema(), indent=None)
    system_full = (
        f"{system}\n\n"
        "Respond with a single JSON object matching this JSON schema exactly. "
        f"No prose, no markdown fences.\nSchema: {schema_json}"
    )
    messages = [
        {"role": "system", "content": system_full},
        {"role": "user", "content": user},
    ]

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = litellm.completion(
                model=cfg["model"],
                messages=messages,
                max_tokens=cfg.get("max_tokens", 2048),
                api_key=api_key,
                api_base=cfg.get("api_base"),
                timeout=timeout,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            last_error = exc
            _record_usage(db, endpoint or role, 0, 0, 0.0, ok=False, detail=str(exc))
            log.warning("llm transport error", attempt=attempt, error=str(exc))
            time.sleep(backoff * attempt)
            continue

        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        try:
            cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            cost = 0.0

        text = resp.choices[0].message.content or ""
        try:
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
            parsed = schema.model_validate_json(cleaned)
            _record_usage(db, endpoint or role, tokens_in, tokens_out, cost, ok=True)
            return parsed
        except (ValidationError, ValueError) as exc:
            last_error = exc
            _record_usage(
                db, endpoint or role, tokens_in, tokens_out, cost, ok=False,
                detail=f"schema validation failed: {exc}",
            )
            messages.append({"role": "assistant", "content": text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "That response did not validate against the schema: "
                        f"{exc}. Return ONLY a corrected JSON object."
                    ),
                }
            )
            time.sleep(backoff * attempt)

    raise LLMError(f"LLM call failed after {max_retries} attempts: {last_error}")


def validate_llm(db: Session) -> ProviderCheck:
    """'Test connection' for the Anthropic key: one tiny triage-role call."""

    class _Ping(BaseModel):
        ok: bool

    try:
        result = complete_json(
            db,
            role="triage",
            system="You are a health check.",
            user='Reply with {"ok": true}',
            schema=_Ping,
            endpoint="validate",
        )
        return ProviderCheck(
            provider="anthropic", ok=result.ok, detail="triage model reachable"
        )
    except LLMError as exc:
        return ProviderCheck(provider="anthropic", ok=False, detail=str(exc))
