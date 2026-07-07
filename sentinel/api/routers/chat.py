"""Chat API (spec §7.1). The assistant has read-only tools; it cannot send
alerts, execute anything, or bypass the risk engine."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel import DISCLAIMER
from sentinel.agents.chat import chat_reply
from sentinel.db.base import get_db
from sentinel.db.models import ChatMessageRow

router = APIRouter(prefix="/api/chat", tags=["chat"])


class MessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@router.post("")
def send(body: MessageIn, db: Session = Depends(get_db)) -> dict:
    result = chat_reply(db, body.message)
    db.commit()
    return {**result, "disclaimer": DISCLAIMER}


@router.get("/history")
def history(limit: int = Query(default=50, le=200), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(ChatMessageRow).order_by(ChatMessageRow.id.desc()).limit(limit)
    ).scalars().all()
    return {
        "messages": [
            {
                "id": r.id,
                "ts": r.ts,
                "role": r.role,
                "content": r.content,
                "tool_name": r.tool_name,
            }
            for r in reversed(rows)
        ],
        "disclaimer": DISCLAIMER,
    }
