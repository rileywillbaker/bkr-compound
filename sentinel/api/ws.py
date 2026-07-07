"""WebSocket live feed (spec §7.4): pushes bus events (new signals, alerts)
to connected browsers. Read-only — the socket accepts no commands that could
touch the pipeline or the risk engine."""

import asyncio
import queue

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from sentinel.api.auth import COOKIE_NAME, auth_enabled, session_valid
from sentinel.data import bus

log = structlog.get_logger()

router = APIRouter()


@router.websocket("/ws")
async def feed(ws: WebSocket) -> None:
    if auth_enabled() and not session_valid(ws.cookies.get(COOKIE_NAME)):
        await ws.close(code=4401)
        return
    await ws.accept()
    q = bus.subscribe()
    try:
        await ws.send_json({"kind": "hello", "payload": {"feed": "signals"}})
        while True:
            try:
                event = await asyncio.to_thread(q.get, True, 25.0)
            except queue.Empty:
                await ws.send_json({"kind": "ping", "payload": {}})
                continue
            event.pop("origin", None)
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover - client-dependent
        log.debug("ws feed closed", error=str(exc))
    finally:
        bus.unsubscribe(q)
