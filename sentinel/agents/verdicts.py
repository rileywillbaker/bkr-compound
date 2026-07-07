"""Shared analyst verdict types (spec §4.3)."""

from datetime import datetime

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    source: str
    datapoint: str
    timestamp: datetime | None = None


class AnalystVerdict(BaseModel):
    analyst: str
    symbol: str
    score: int = Field(ge=-100, le=100)
    confidence: float = Field(ge=0, le=1)
    summary: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)
    deterministic_only: bool = False  # True when LLM was skipped/unavailable
    unavailable: bool = False  # True when the factor could not be assessed


class LLMVerdictPayload(BaseModel):
    """What the LLM is asked to return. Numbers here are OPINIONS (scores),
    never trade parameters."""

    score: int = Field(ge=-100, le=100)
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(max_length=600)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=10)
