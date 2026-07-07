"""Live event bus for the web app's WebSocket feed (spec §7.4).

Events (new signals, alerts, pipeline runs) are published to Redis pub/sub so
the worker process can reach API-process WebSocket clients; an in-process
broker mirrors every publish so the feed also works without Redis (dev/tests).
Publishing must never break the pipeline — all failures are swallowed and
logged.
"""

import contextlib
import json
import queue
import threading
from datetime import UTC, datetime
from typing import Any

import structlog

from sentinel.config import get_settings

log = structlog.get_logger()

CHANNEL = "bquant:feed"

# Identifies this process so the Redis relay can skip events we published
# ourselves (local subscribers already received them directly).
_PROCESS_ID = __import__("uuid").uuid4().hex

_subscribers: list[queue.Queue] = []
_lock = threading.Lock()


def subscribe() -> queue.Queue:
    """Register an in-process subscriber. Caller must unsubscribe()."""
    q: queue.Queue = queue.Queue(maxsize=256)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


def publish(kind: str, payload: dict[str, Any]) -> None:
    event = {
        "kind": kind,
        "ts": datetime.now(UTC).isoformat(),
        "payload": payload,
        "origin": _PROCESS_ID,
    }
    with _lock:
        targets = list(_subscribers)
    for q in targets:
        try:
            q.put_nowait(event)
        except queue.Full:  # slow consumer: drop rather than block the pipeline
            log.warning("bus subscriber queue full; dropping event", kind=kind)
    try:
        import redis as redis_lib

        r = redis_lib.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)
        r.publish(CHANNEL, json.dumps(event, default=str))
    except Exception:
        # Redis optional in dev; in-process subscribers already got the event.
        log.debug("redis publish skipped", kind=kind)


def start_redis_relay(stop: threading.Event) -> threading.Thread | None:
    """Relay events published by OTHER processes (the worker) into this
    process's in-process subscribers. Returns None if Redis is unreachable."""
    try:
        import redis as redis_lib

        r = redis_lib.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2)
        pubsub = r.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(CHANNEL)
    except Exception:
        log.info("redis relay not started (redis unreachable) — local events only")
        return None

    def _run() -> None:
        while not stop.is_set():
            try:
                msg = pubsub.get_message(timeout=1.0)
            except Exception:
                continue
            if msg and msg.get("type") == "message":
                try:
                    event = json.loads(msg["data"])
                except (ValueError, TypeError):
                    continue
                if event.get("origin") == _PROCESS_ID:
                    continue
                with _lock:
                    targets = list(_subscribers)
                for q in targets:
                    with contextlib.suppress(queue.Full):
                        q.put_nowait(event)
        pubsub.close()

    thread = threading.Thread(target=_run, name="bus-redis-relay", daemon=True)
    thread.start()
    return thread
