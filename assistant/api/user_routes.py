"""Account administration (admins only).

Admins create, disable and remove accounts and reset passwords. They never see another
user's data: nothing here reads conversations, memory, mail or reminders, and the data
endpoints only ever serve the signed-in user's own rows.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..core.models import AuthSession, User
from ..core.security import verify_password
from ..core.users import CurrentUser, UserError, temporary_password
from .auth import TOTP_KEYS, container, drop_sessions, require_admin

router = APIRouter(prefix="/api/users", dependencies=[Depends(require_admin)])


def _view(user: User, sessions: int = 0, owner: int | None = None) -> dict[str, Any]:
    return {
        "id": user.id, "username": user.username, "display_name": user.display_name,
        "role": user.role, "disabled": user.disabled, "owner": user.id == owner,
        "must_change_password": user.must_change_password, "sessions": sessions,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


class NewUser(BaseModel):
    username: str
    display_name: str = ""
    role: str = "member"
    # Leave empty to have one generated; it is shown once.
    password: str = Field(default="", max_length=200)


class UserPatch(BaseModel):
    display_name: str | None = None
    role: str | None = None
    disabled: bool | None = None
    # The admin's own password, needed to change who is an admin.
    password: str = ""


class Reauth(BaseModel):
    password: str


async def _admin_password_ok(request: Request, admin: CurrentUser, password: str) -> None:
    c = container(request)
    c.login_guard.check(admin.username)
    row = await c.users.get(admin.id)
    if row is None or not verify_password(password, row.password_hash):
        c.login_guard.fail(admin.username)
        raise HTTPException(403, "Wrong password")


async def _get(request: Request, user_id: int) -> User:
    user = await container(request).users.get(user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    return user


@router.get("")
async def list_users(request: Request):
    c = container(request)
    owner = await c.users.owner_id()
    async with c.db.session() as s:
        counts = dict(
            (
                await s.execute(
                    select(AuthSession.user_id, func.count()).group_by(AuthSession.user_id)
                )
            ).all()
        )
    return [_view(u, counts.get(u.id, 0), owner) for u in await c.users.list()]


@router.post("", status_code=201)
async def create_user(
    body: NewUser, request: Request, admin: CurrentUser = Depends(require_admin)
):
    """Create an account. The person must change the password the first time they sign in."""
    c = container(request)
    password = body.password or temporary_password()
    try:
        user = await c.users.create(
            body.username, password, role=body.role, display_name=body.display_name,
            must_change_password=True,
        )
    except UserError as e:
        raise HTTPException(422, str(e)) from None
    await c.tenants.add(user)
    await c.of(admin.id).audit.record(
        "user", "user.create", f"Created the account {user.username} ({user.role})", change=True
    )
    await c.of(user.id).audit.record(
        "user", "user.created", f"Account created by {admin.username}", change=True
    )
    # Echoed back only when it was generated: the admin has to hand it over.
    return {"user": _view(user, 0, await c.users.owner_id()),
            "temporary_password": None if body.password else password}


@router.patch("/{user_id}")
async def update_user(
    user_id: int, body: UserPatch, request: Request, admin: CurrentUser = Depends(require_admin)
):
    c = container(request)
    target = await _get(request, user_id)
    changes_access = (body.role is not None and body.role != target.role) or body.disabled is True
    if changes_access:
        if user_id == admin.id:
            raise HTTPException(409, "You cannot change your own role or disable yourself")
        await _admin_password_ok(request, admin, body.password)
    try:
        user = await c.users.update(
            user_id, role=body.role, disabled=body.disabled, display_name=body.display_name
        )
    except UserError as e:
        raise HTTPException(409, str(e)) from None
    await c.tenants.sync_account(user)
    if body.disabled:
        await drop_sessions(c, user_id)
    notes = []
    if body.role is not None and body.role != target.role:
        notes.append(f"role is now {user.role}")
    if body.disabled is not None and body.disabled != target.disabled:
        notes.append("disabled" if user.disabled else "enabled")
    if notes:
        what = f"Account {user.username}: {', '.join(notes)}"
        await c.of(admin.id).audit.record("user", "user.update", what, change=True)
        await c.of(user.id).audit.record("user", "user.updated", f"{what} (by {admin.username})", change=True)
    return _view(user, owner=await c.users.owner_id())


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: int, body: Reauth, request: Request, admin: CurrentUser = Depends(require_admin)
):
    """Give someone a new password (they must change it when they sign in) and sign them
    out everywhere. Shown once."""
    c = container(request)
    target = await _get(request, user_id)
    await _admin_password_ok(request, admin, body.password)
    password = temporary_password()
    await c.users.set_password(user_id, password, must_change_password=True)
    await drop_sessions(c, user_id)
    await c.of(admin.id).audit.record(
        "user", "user.reset_password", f"Reset the password of {target.username}", change=True
    )
    await c.of(user_id).audit.record(
        "user", "user.password_reset", f"Your password was reset by {admin.username}", change=True
    )
    return {"temporary_password": password}


@router.post("/{user_id}/reset-two-step")
async def reset_two_step(
    user_id: int, body: Reauth, request: Request, admin: CurrentUser = Depends(require_admin)
):
    """For someone who lost their authenticator and recovery codes."""
    c = container(request)
    target = await _get(request, user_id)
    await _admin_password_ok(request, admin, body.password)
    for key in TOTP_KEYS:
        await c.of(user_id).settings.delete(key)
    await drop_sessions(c, user_id)
    await c.of(admin.id).audit.record(
        "user", "user.reset_totp", f"Turned off two-step sign-in for {target.username}", change=True
    )
    await c.of(user_id).audit.record(
        "user", "user.totp_reset", f"Two-step sign-in was turned off by {admin.username}", change=True
    )
    return {"ok": True}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int, body: Reauth, request: Request, admin: CurrentUser = Depends(require_admin)
):
    """Delete the account and everything it owns. Cannot be undone."""
    c = container(request)
    target = await _get(request, user_id)
    if user_id == admin.id:
        raise HTTPException(409, "You cannot delete your own account")
    await _admin_password_ok(request, admin, body.password)
    scoped = c.of(user_id).settings
    try:
        await c.users.check_deletable(user_id)  # the owner and the last admin stay
    except UserError as e:
        raise HTTPException(409, str(e)) from None
    await c.tenants.delete_files(user_id)
    await c.tenants.remove(user_id)
    await c.users.delete(user_id)  # their rows go with it (foreign keys cascade)
    await scoped.delete_all()
    await c.of(admin.id).audit.record(
        "user", "user.delete", f"Deleted the account {target.username} and all its data", change=True
    )
    return {"ok": True}
