"""Upgrading a single-user database to the multi-user schema keeps everything, and gives it
to the first account."""

from __future__ import annotations

import json
import sqlite3

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from assistant.core.db import MIGRATIONS_DIR
from assistant.core.models import Base
from assistant.core.security import verify_password

NOW = "2026-03-10 12:00:00.000000"


def _upgrade(path, revision: str) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, revision)


def _seed(db: sqlite3.Connection, with_password: bool) -> None:
    from assistant.core.security import hash_password

    def setting(key, value):
        db.execute("INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
                   (key, json.dumps(value), NOW))

    if with_password:
        setting("auth.password_hash", hash_password("an old password"))
        setting("auth.totp_secret", "enc")
        setting("auth.recovery_hashes", ["x"])
        setting("briefing.latest", {"date": "2026-03-09"})
        setting("config:briefing.time", "06:45")
        setting("config:notifications.telegram.chat_id", "42")
        setting("config:notifications.telegram.bot_token", "123:abc")  # stays server-wide
        setting("config:llm.chat.model", "m")  # stays server-wide
        setting("webpush.vapid_private_key", "pem")  # stays server-wide
    db.execute("INSERT INTO conversations (id, title, channel, summary, summary_upto_id, "
               "summary_tainted, created_at, updated_at) VALUES (1, 'hi', 'web', '', 0, 0, ?, ?)", (NOW, NOW))
    db.execute("INSERT INTO messages (conversation_id, role, content, meta, created_at) "
               "VALUES (1, 'user', 'hello', '{}', ?)", (NOW,))
    db.execute("INSERT INTO memory_facts (category, content, source, created_at, updated_at) "
               "VALUES ('general', 'likes tea', 'user', ?, ?)", (NOW, NOW))
    db.execute("INSERT INTO embeddings (kind, ref_id, text, model, dim, vector, meta, created_at) "
               "VALUES ('fact', '1', 'likes tea', 'm', 1, x'00000000', '{}', ?)", (NOW,))
    db.execute("INSERT INTO items (plugin, external_id, type, title, body, participants, labels, data, "
               "content_hash, created_at, updated_at) VALUES ('gmail', 'm1', 'message', 't', 'b', '[]', "
               "'[]', '{}', 'h', ?, ?)", (NOW, NOW))
    db.execute("INSERT INTO plugins (name, enabled, config, secrets, webhook_token, updated_at) "
               "VALUES ('gmail', 1, '{}', 'sealed', 'tok', ?)", (NOW,))
    db.execute("INSERT INTO plugin_kv (plugin, key, value) VALUES ('gmail', 'cursor', '\"c1\"')")
    db.execute("INSERT INTO reminders (text, due_at, next_at, anchor, timezone, status, urgency, source, "
               "fire_count, created_at, updated_at) VALUES ('call', ?, ?, '', 'UTC', 'scheduled', "
               "'normal', 'user', 0, ?, ?)", (NOW, NOW, NOW, NOW))
    db.execute("INSERT INTO notifications (category, title, body, urgency, status, results, created_at) "
               "VALUES ('system', 'n', '', 'normal', 'sent', '{}', ?)", (NOW,))
    db.execute("INSERT INTO suggestions (category, title, detail, reason, confidence, source, dedupe_key, "
               "status, created_at) VALUES ('bill', 's', '', '', 0.9, 'rule', 'bill:1', 'pending', ?)", (NOW,))
    db.execute("INSERT INTO audit_log (at, actor, action, summary, change, detail) "
               "VALUES (?, 'user', 'x', 's', 0, '{}')", (NOW,))
    db.execute("INSERT INTO push_subscriptions (endpoint_hash, data, label, created_at) "
               "VALUES ('h', 'd', 'phone', ?)", (NOW,))
    db.execute("INSERT INTO auth_sessions (token_hash, created_at, expires_at) VALUES ('t', ?, '2999-01-01')", (NOW,))
    db.commit()


def test_existing_data_becomes_the_first_accounts(tmp_path):
    path = tmp_path / "old.db"
    _upgrade(path, "0006")
    db = sqlite3.connect(path)
    _seed(db, with_password=True)
    db.close()

    _upgrade(path, "head")
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []

    [(uid, name, role, pw, must_change)] = db.execute(
        "SELECT id, username, role, password_hash, must_change_password FROM users").fetchall()
    assert (uid, name, role, must_change) == (1, "owner", "admin", 0)
    assert verify_password("an old password", pw)  # the old password still works

    for table in ("conversations", "memory_facts", "embeddings", "items", "plugins", "plugin_kv",
                  "reminders", "notifications", "suggestions", "audit_log", "push_subscriptions",
                  "auth_sessions"):
        assert db.execute(f"SELECT user_id FROM {table}").fetchall() == [(1,)], table
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)
    assert db.execute("SELECT webhook_token, secrets FROM plugins").fetchone() == ("tok", "sealed")
    assert db.execute("SELECT value FROM plugin_kv").fetchone() == ('"c1"',)

    keys = {k for (k,) in db.execute("SELECT key FROM settings")}
    assert "auth.password_hash" not in keys
    assert {"user:1:auth.totp_secret", "user:1:auth.recovery_hashes", "user:1:briefing.latest",
            "user:1:config:briefing.time", "user:1:config:notifications.telegram.chat_id"} <= keys
    # Server-wide settings stay where they were.
    assert {"config:notifications.telegram.bot_token", "config:llm.chat.model",
            "webpush.vapid_private_key"} <= keys

    # The same text can now exist for two users, and cascades work.
    db.execute("INSERT INTO users (id, username, display_name, role, password_hash, must_change_password,"
               " disabled, created_at) VALUES (2, 'b', '', 'member', 'x', 0, 0, ?)", (NOW,))
    db.execute("INSERT INTO items (user_id, plugin, external_id, type, title, body, participants, labels,"
               " data, content_hash, created_at, updated_at) VALUES (2, 'gmail', 'm1', 'message', 't', 'b',"
               " '[]', '[]', '{}', 'h', ?, ?)", (NOW, NOW))
    db.execute("DELETE FROM users WHERE id = 1")
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM items").fetchone() == (1,)


def test_a_database_that_never_finished_setup_just_upgrades(tmp_path):
    path = tmp_path / "unfinished.db"
    _upgrade(path, "0006")
    db = sqlite3.connect(path)
    # Plugin rows exist before the password is set; nothing else does.
    db.execute("INSERT INTO plugins (name, enabled, config, secrets, webhook_token, updated_at) "
               "VALUES ('gmail', 0, '{}', '', 'tok', ?)", (NOW,))
    db.commit()
    db.close()
    _upgrade(path, "head")
    db = sqlite3.connect(path)
    assert db.execute("SELECT COUNT(*) FROM users").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM plugins").fetchone() == (0,)


def test_the_migrated_schema_matches_the_models(tmp_path):
    path = tmp_path / "check.db"
    _upgrade(path, "head")
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []
