"""REST API for notifications forwarded by the Android app."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .auth import mine, require_auth

router = APIRouter(prefix="/api/phone", dependencies=[Depends(require_auth)])


class PhoneNotification(BaseModel):
    # The phone's own id for it, only used to tell two notifications apart.
    key: str = Field(default="", max_length=300)
    package: str = Field(max_length=200)
    app: str = Field(default="", max_length=100)
    title: str = Field(default="", max_length=1000)
    text: str = Field(default="", max_length=10000)
    # Epoch milliseconds.
    posted_at: int
    category: str = Field(default="", max_length=40)


class Batch(BaseModel):
    notifications: list[PhoneNotification] = Field(max_length=50)


@router.post("/notifications")
async def receive(body: Batch, request: Request):
    phone = mine(request).phone
    if not phone.enabled:
        raise HTTPException(403, "Phone notifications are turned off for your account on this server.")
    return await phone.ingest([n.model_dump() for n in body.notifications])


@router.get("/notifications")
async def recent(request: Request, limit: int = 50):
    phone = mine(request).phone
    return {"enabled": phone.enabled, "notifications": await phone.recent(limit)}


@router.delete("/notifications/{item_id}")
async def remove(item_id: int, request: Request):
    if not await mine(request).phone.delete(item_id):
        raise HTTPException(404, "No such notification")
    return {"ok": True}


@router.delete("/notifications")
async def clear(request: Request):
    t = mine(request)
    deleted = await t.phone.clear()
    await t.audit.record("user", "phone.clear", f"Deleted {deleted} phone notifications", change=True)
    return {"deleted": deleted}
