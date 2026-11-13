"""REST API: suggestion inbox, feedback, and the audit log."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..proactive.engine import ProactiveError
from .auth import container, require_auth

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


class AcceptBody(BaseModel):
    # "reminder": turn a calendar-event proposal into a reminder instead.
    mode: Literal["reminder"] | None = None


class FeedbackBody(BaseModel):
    useful: bool | None  # null clears the feedback


async def _do(call):
    try:
        return await call
    except LookupError:
        raise HTTPException(404, "Suggestion not found") from None
    except ProactiveError as e:
        raise HTTPException(409, str(e)) from None


@router.get("/suggestions")
async def list_suggestions(request: Request, history: bool = False):
    c = container(request)
    return {
        "suggestions": await c.proactive.list(history=history),
        "pending": await c.proactive.count(),
        "enabled": c.config.config.proactive.enabled,
    }


@router.get("/suggestions/count")
async def count_suggestions(request: Request):
    return {"pending": await container(request).proactive.count()}


@router.get("/suggestions/learning")
async def learning(request: Request):
    return await container(request).proactive.learning()


@router.post("/suggestions/check")
async def check_now(request: Request):
    """Run the proactive checks immediately instead of waiting for the next tick."""
    c = container(request)
    if not c.config.config.proactive.enabled:
        raise HTTPException(409, "Proactive suggestions are turned off in Settings")
    return await c.proactive.run()


@router.post("/suggestions/{suggestion_id}/accept")
async def accept(suggestion_id: int, request: Request, body: AcceptBody | None = None):
    return await _do(container(request).proactive.accept(suggestion_id, body.mode if body else None))


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss(suggestion_id: int, request: Request):
    return await _do(container(request).proactive.dismiss(suggestion_id))


@router.post("/suggestions/{suggestion_id}/feedback")
async def feedback(suggestion_id: int, body: FeedbackBody, request: Request):
    return await _do(container(request).proactive.feedback(suggestion_id, body.useful))


@router.get("/audit")
async def audit_log(request: Request, changes: bool = False, limit: int = 100, before: int | None = None):
    return await container(request).audit.list(changes_only=changes, limit=limit, before_id=before)


@router.post("/audit/{entry_id}/undo")
async def undo(entry_id: int, request: Request):
    c = container(request)
    try:
        return await c.audit.undo(entry_id, c.tools)
    except LookupError:
        raise HTTPException(404, "Entry not found") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
