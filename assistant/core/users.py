"""Accounts: who can sign in, and what they may administer.

The first account (the owner) is an admin. Admins manage accounts and the server-wide
settings; they cannot read anyone else's conversations, memory, mail or reminders, because
that data is scoped to its owner in every query (see ``docs/SECURITY.md``).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select

from .db import Database
from .models import User, utcnow
from .security import hash_password

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
ROLES = ("admin", "member")


class UserError(ValueError):
    """A request about accounts that cannot be done; the message says why."""


@dataclass(frozen=True)
class CurrentUser:
    """The signed-in account, as handed to request handlers."""

    id: int
    username: str
    display_name: str
    role: str
    must_change_password: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def label(self) -> str:
        return self.display_name or self.username

    @classmethod
    def of(cls, user: User) -> CurrentUser:
        return cls(
            user.id, user.username, user.display_name, user.role, user.must_change_password
        )

    def view(self) -> dict[str, object]:
        return {
            "id": self.id, "username": self.username, "display_name": self.display_name,
            "role": self.role, "must_change_password": self.must_change_password,
        }


def clean_username(value: str) -> str:
    name = (value or "").strip().lower()
    if not USERNAME.match(name):
        raise UserError(
            "A username is 1-32 characters: lower-case letters, digits, dots, dashes and "
            "underscores, starting with a letter or digit."
        )
    return name


def temporary_password() -> str:
    """A random password for an admin to hand over; the user must change it at first sign-in."""
    return secrets.token_urlsafe(12)


class UserService:
    def __init__(self, db: Database, min_password_length: Callable[[], int]):
        self._db = db
        self._min_length = min_password_length

    def check_password(self, password: str) -> None:
        if len(password) < self._min_length():
            raise UserError(f"Password must be at least {self._min_length()} characters")

    async def count(self) -> int:
        async with self._db.session() as s:
            return await s.scalar(select(func.count()).select_from(User)) or 0

    async def get(self, user_id: int) -> User | None:
        async with self._db.session() as s:
            return await s.get(User, user_id)

    async def by_username(self, username: str) -> User | None:
        async with self._db.session() as s:
            return await s.scalar(select(User).where(User.username == username.strip().lower()))

    async def list(self) -> list[User]:
        async with self._db.session() as s:
            return list((await s.scalars(select(User).order_by(User.id))).all())

    async def owner_id(self) -> int | None:
        """The first account ever created."""
        async with self._db.session() as s:
            return await s.scalar(select(func.min(User.id)))

    async def only_user(self) -> User | None:
        """The one enabled account, when there is exactly one (single-user installs and the
        older Android client sign in without naming it)."""
        users = await self.list()
        return users[0] if len(users) == 1 and not users[0].disabled else None

    async def create(
        self, username: str, password: str, *, role: str = "member", display_name: str = "",
        must_change_password: bool = False,
    ) -> User:
        name = clean_username(username)
        if role not in ROLES:
            raise UserError("role must be admin or member")
        self.check_password(password)
        async with self._db.session() as s:
            if await s.scalar(select(User.id).where(User.username == name)):
                raise UserError(f"The username {name!r} is taken")
            user = User(
                username=name, display_name=display_name.strip()[:100], role=role,
                password_hash=hash_password(password),
                must_change_password=must_change_password,
            )
            s.add(user)
            await s.commit()
        return user

    async def set_password(
        self, user_id: int, password: str, *, must_change_password: bool = False
    ) -> None:
        self.check_password(password)
        async with self._db.session() as s:
            user = await s.get(User, user_id)
            if user is None:
                raise UserError("No such user")
            user.password_hash = hash_password(password)
            user.must_change_password = must_change_password
            await s.commit()

    async def _other_admins(self, s, user_id: int) -> int:
        return await s.scalar(
            select(func.count()).select_from(User).where(
                User.role == "admin", User.disabled.is_(False), User.id != user_id
            )
        ) or 0

    async def update(
        self, user_id: int, *, role: str | None = None, disabled: bool | None = None,
        display_name: str | None = None,
    ) -> User:
        async with self._db.session() as s:
            user = await s.get(User, user_id)
            if user is None:
                raise UserError("No such user")
            leaves_admin = user.role == "admin" and not user.disabled and (
                (role is not None and role != "admin") or disabled is True
            )
            if leaves_admin and not await self._other_admins(s, user_id):
                raise UserError("There must be at least one active admin")
            if role is not None:
                if role not in ROLES:
                    raise UserError("role must be admin or member")
                user.role = role
            if disabled is not None:
                user.disabled = disabled
            if display_name is not None:
                user.display_name = display_name.strip()[:100]
            await s.commit()
        return user

    async def check_deletable(self, user_id: int) -> None:
        """Raise UserError unless the account may be deleted."""
        async with self._db.session() as s:
            user = await s.get(User, user_id)
            if user is None:
                raise UserError("No such user")
            if user_id == await s.scalar(select(func.min(User.id))):
                raise UserError("The owner account cannot be deleted")
            if user.role == "admin" and not user.disabled and not await self._other_admins(s, user_id):
                raise UserError("There must be at least one active admin")

    async def delete(self, user_id: int) -> None:
        """Remove the account; every row it owns goes with it (foreign keys cascade)."""
        await self.check_deletable(user_id)
        async with self._db.session() as s:
            user = await s.get(User, user_id)
            assert user is not None
            await s.delete(user)
            await s.commit()

    async def touch_login(self, user_id: int) -> None:
        async with self._db.session() as s:
            user = await s.get(User, user_id)
            if user is not None:
                user.last_login_at = utcnow()
                await s.commit()
