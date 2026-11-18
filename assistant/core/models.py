"""SQLAlchemy models (Phase 1: conversations, memory, embeddings, settings, sessions)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    """An account. The first one created is the owner and an admin."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Lower-case, 1-32 characters from [a-z0-9._-].
    username: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    role: Mapped[str] = mapped_column(String(10), default="member")  # admin | member
    password_hash: Mapped[str] = mapped_column(Text)
    # An admin set or reset the password: only changing it is allowed until then.
    must_change_password: Mapped[bool] = mapped_column(default=False, server_default="0")
    disabled: Mapped[bool] = mapped_column(default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def owner_id() -> Any:
    """Column definition shared by every per-user table."""
    return mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    title: Mapped[str] = mapped_column(String(200), default="")
    channel: Mapped[str] = mapped_column(String(50), default="web")
    # Rolling summary of every message with id <= summary_upto_id.
    summary: Mapped[str] = mapped_column(Text, default="")
    summary_upto_id: Mapped[int] = mapped_column(default=0)
    # The summary was written from turns that involved outside content.
    summary_tainted: Mapped[bool] = mapped_column(default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    # Model used, tool calls made during the turn, attachments, etc.
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class MemoryFact(Base):
    """A long-term fact or preference about the user."""

    __tablename__ = "memory_facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    category: Mapped[str] = mapped_column(String(50), default="general")
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="user")  # user | agent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Embedding(Base):
    """A text chunk and its vector (float32 little-endian blob)."""

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "ref_id", name="uq_embeddings_user_kind_ref"),
        Index("ix_embeddings_model", "model"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    kind: Mapped[str] = mapped_column(String(30))  # fact | message | (later) item
    ref_id: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    dim: Mapped[int] = mapped_column()
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    """Key/value store for runtime settings; secret values are encrypted."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    encrypted: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = owner_id()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PluginState(Base):
    """Persistent per-plugin state: enabled flag, config, encrypted secrets."""

    __tablename__ = "plugins"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Fernet-encrypted JSON object: declared secrets plus OAuth tokens under "_oauth".
    secrets: Mapped[str] = mapped_column(Text, default="")
    webhook_token: Mapped[str] = mapped_column(String(64), default="")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PluginKV(Base):
    """Namespaced key/value storage for plugins (sync cursors, caches)."""

    __tablename__ = "plugin_kv"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    plugin: Mapped[str] = mapped_column(String(50), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class Item(Base):
    """Common data model: everything plugins sync is normalized into this table."""

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("user_id", "plugin", "external_id", name="uq_items_user_plugin_external"),
        Index("ix_items_type_starts", "type", "starts_at"),
        Index("ix_items_occurred", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    plugin: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(300))
    # item | event | task | message | contact | document | transaction | location
    type: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    participants: Mapped[list[str]] = mapped_column(JSON, default=list)
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PendingAction(Base):
    """A side-effecting tool call waiting for the user's approval."""

    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # pending | done | failed | rejected
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Reminder(Base):
    """A one-off or recurring reminder.

    ``due_at`` is the occurrence shown to the user; ``next_at`` is when the scheduler
    fires next (differs from ``due_at`` while snoozed, NULL when nothing is scheduled).
    """

    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_status_next", "status", "next_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # RFC 5545 RRULE (without the "RRULE:" prefix), evaluated in `timezone` wall-clock time.
    rrule: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # First occurrence in local wall-clock time (ISO, no offset): the recurrence anchor.
    anchor: Mapped[str] = mapped_column(String(30), default="")
    timezone: Mapped[str] = mapped_column(String(60), default="UTC")
    # scheduled | fired (notified, waiting for done/snooze) | done | cancelled
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    urgency: Mapped[str] = mapped_column(String(10), default="normal")
    # user (UI) | agent (chat) | external (agent, while third-party content was in play)
    source: Mapped[str] = mapped_column(String(20), default="user")
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fire_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationRecord(Base):
    """Every notification: the in-app inbox, the held (quiet hours) queue and the log."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_status_after", "status", "deliver_after"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    category: Mapped[str] = mapped_column(String(30), default="system")
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    urgency: Mapped[str] = mapped_column(String(10), default="normal")
    # What it is about, e.g. "reminder:12" (drives the Done / Snooze buttons).
    ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # held | sent | failed
    status: Mapped[str] = mapped_column(String(20), default="held")
    deliver_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-channel outcome: {"telegram": "ok", "email": "error: ..."}
    results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PushSubscription(Base):
    """A browser's Web Push subscription. ``data`` is the encrypted subscription JSON."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    endpoint_hash: Mapped[str] = mapped_column(String(64), unique=True)
    data: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Attachment(Base):
    """An uploaded image. The bytes live in ``<data_dir>/attachments/<stored_name>``."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    # NULL until the message it was uploaded for is sent.
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), default="image")
    mime: Mapped[str] = mapped_column(String(100))
    filename: Mapped[str] = mapped_column(String(200), default="")
    stored_name: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(default=0)
    # What a separate vision model saw (used instead of the image in later turns).
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Suggestion(Base):
    """Something the proactive engine noticed: the suggestion inbox and its history."""

    __tablename__ = "suggestions"
    __table_args__ = (
        Index("ix_suggestions_status_created", "status", "created_at"),
        UniqueConstraint("user_id", "dedupe_key", name="uq_suggestions_user_dedupe"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    # bill | deadline | appointment | commitment | travel | package | needs_reply |
    # calendar_conflict | back_to_back | early_meeting | meeting_brief
    category: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")
    # "Why am I seeing this?"
    reason: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source: Mapped[str] = mapped_column(String(10), default="rule")  # rule | llm
    # What accepting does: {"type": "reminder"|"event"|"chat", ...}; NULL = information only.
    proposed: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    item_id: Mapped[int | None] = mapped_column(nullable=True)
    sender: Mapped[str | None] = mapped_column(String(200), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(300))
    # pending | accepted | auto_applied | dismissed | expired
    status: Mapped[str] = mapped_column(String(20), default="pending")
    feedback: Mapped[str | None] = mapped_column(String(12), nullable=True)  # useful | not_useful
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProactiveMark(Base):
    """Which version of an item the proactive engine has already looked at."""

    __tablename__ = "proactive_marks"

    item_id: Mapped[int] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEntry(Base):
    """Everything the assistant did on the user's behalf."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_at", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = owner_id()
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(20))  # assistant | proactive | user
    action: Mapped[str] = mapped_column(String(80))  # e.g. tool.create_reminder
    summary: Mapped[str] = mapped_column(Text)
    # True when something was created, changed, sent or deleted (not just read).
    change: Mapped[bool] = mapped_column(default=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # How to reverse it: {"tool": ..., "arguments": {...}, "label": ...}; NULL = cannot.
    undo: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
