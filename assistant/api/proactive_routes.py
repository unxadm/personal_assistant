"""REST API: suggestion inbox, feedback, and the audit log."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..proactive.engine import ProactiveError
from .auth import mine, require_auth

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
    t = mine(request)
    return {
        "suggestions": await t.proactive.list(history=history),
        "pending": await t.proactive.count(),
        "enabled": t.cfg().proactive.enabled,
    }


@router.get("/suggestions/count")
async def count_suggestions(request: Request):
    return {"pending": await mine(request).proactive.count()}


@router.get("/suggestions/learning")
async def learning(request: Request):
    return await mine(request).proactive.learning()


@router.post("/suggestions/check")
async def check_now(request: Request):
    """Run the proactive checks immediately instead of waiting for the next tick."""
    t = mine(request)
    if not t.cfg().proactive.enabled:
        raise HTTPException(409, "Proactive suggestions are turned off in Settings")
    return await t.proactive.run()


@router.post("/suggestions/{suggestion_id}/accept")
async def accept(suggestion_id: int, request: Request, body: AcceptBody | None = None):
    return await _do(mine(request).proactive.accept(suggestion_id, body.mode if body else None))


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss(suggestion_id: int, request: Request):
    return await _do(mine(request).proactive.dismiss(suggestion_id))


@router.post("/suggestions/{suggestion_id}/feedback")
async def feedback(suggestion_id: int, body: FeedbackBody, request: Request):
    return await _do(mine(request).proactive.feedback(suggestion_id, body.useful))


@router.get("/audit")
async def audit_log(request: Request, changes: bool = False, limit: int = 100, before: int | None = None):
    return await mine(request).audit.list(changes_only=changes, limit=limit, before_id=before)


@router.post("/audit/{entry_id}/undo")
async def undo(entry_id: int, request: Request):
    t = mine(request)
    try:
        return await t.audit.undo(entry_id, t.tools)
    except LookupError:
        raise HTTPException(404, "Entry not found") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
