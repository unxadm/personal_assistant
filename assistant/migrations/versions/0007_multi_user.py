"""multi-user: users table, user_id on every per-user table

Existing data becomes the first user's ("owner", an admin) with the password, two-step
sign-in and settings that used to be global. A database without a password yet (first run
never finished) has nothing to keep, so its per-user rows are dropped.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17 09:00:00
"""
import json

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

OWNER = 1
# Tables that only gain a user_id column.
SIMPLE = [
    'conversations', 'memory_facts', 'pending_actions', 'reminders', 'notifications',
    'push_subscriptions', 'attachments', 'audit_log', 'auth_sessions',
]
# Settings that belonged to the one user and now live under "user:<id>:".
PER_USER_KEYS = [
    'auth.totp_secret', 'auth.totp_pending', 'auth.totp_last_counter', 'auth.recovery_hashes',
    'telegram.conversation_id', 'briefing.latest', 'briefing.history', 'briefing.last_date',
]
USER_CONFIG = (
    'privacy.', 'notifications.', 'briefing.', 'proactive.', 'timezone',
)
# Settings that stay server-wide, even though they sit under a per-user prefix.
SERVER_WIDE = (
    'notifications.telegram.bot_token', 'notifications.telegram.api_base',
    'notifications.web_push.contact',
)


def _fk(name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=name, ondelete='CASCADE')


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=32), nullable=False),
        sa.Column('display_name', sa.String(length=100), nullable=False),
        sa.Column('role', sa.String(length=10), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('must_change_password', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('disabled', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username'),
    )
    bind = op.get_bind()
    settings = sa.table(
        'settings', sa.column('key', sa.String), sa.column('value', sa.JSON),
        sa.column('encrypted', sa.Boolean),
    )
    row = bind.execute(
        sa.select(settings.c.value).where(settings.c.key == 'auth.password_hash')
    ).first()
    has_owner = row is not None
    if has_owner:
        bind.execute(
            sa.text(
                'INSERT INTO users (id, username, display_name, role, password_hash, '
                'must_change_password, disabled, created_at) '
                "VALUES (:id, 'owner', 'Owner', 'admin', :hash, 0, 0, CURRENT_TIMESTAMP)"
            ),
            {'id': OWNER, 'hash': row[0] if isinstance(row[0], str) else json.loads(row[0])},
        )
        bind.execute(settings.delete().where(settings.c.key == 'auth.password_hash'))
        for key in PER_USER_KEYS:
            bind.execute(
                settings.update().where(settings.c.key == key).values(key=f'user:{OWNER}:{key}')
            )
        # Saved overrides of personal settings follow the owner; the rest stay server-wide.
        for (key,) in bind.execute(
            sa.select(settings.c.key).where(settings.c.key.like('config:%'))
        ).all():
            path = key[len('config:'):]
            if path.startswith(USER_CONFIG) and path not in SERVER_WIDE:
                bind.execute(
                    settings.update().where(settings.c.key == key)
                    .values(key=f'user:{OWNER}:{key}')
                )

    # -- tables that only gain a column ------------------------------------------------
    for table in SIMPLE:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        if has_owner:
            bind.execute(sa.text(f'UPDATE {table} SET user_id = :u'), {'u': OWNER})
        else:
            bind.execute(sa.text(f'DELETE FROM {table}'))
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=False)
            batch_op.create_foreign_key(f'fk_{table}_user', 'users', ['user_id'], ['id'], ondelete='CASCADE')
            batch_op.create_index(batch_op.f(f'ix_{table}_user_id'), ['user_id'], unique=False)

    # -- tables whose key or unique constraint changes: rebuild them -------------------
    _rebuild(
        'items',
        [
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('plugin', sa.String(length=50), nullable=False),
            sa.Column('external_id', sa.String(length=300), nullable=False),
            sa.Column('type', sa.String(length=20), nullable=False),
            sa.Column('title', sa.Text(), nullable=False),
            sa.Column('body', sa.Text(), nullable=False),
            sa.Column('url', sa.Text(), nullable=True),
            sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('participants', sa.JSON(), nullable=False),
            sa.Column('labels', sa.JSON(), nullable=False),
            sa.Column('data', sa.JSON(), nullable=False),
            sa.Column('content_hash', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            _fk('fk_items_user'),
            sa.UniqueConstraint('user_id', 'plugin', 'external_id', name='uq_items_user_plugin_external'),
        ],
        has_owner,
        indexes=[
            ('ix_items_occurred', ['occurred_at']),
            ('ix_items_type_starts', ['type', 'starts_at']),
            ('ix_items_user_id', ['user_id']),
        ],
    )
    _rebuild(
        'embeddings',
        [
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('kind', sa.String(length=30), nullable=False),
            sa.Column('ref_id', sa.String(length=100), nullable=False),
            sa.Column('text', sa.Text(), nullable=False),
            sa.Column('model', sa.String(length=200), nullable=False),
            sa.Column('dim', sa.Integer(), nullable=False),
            sa.Column('vector', sa.LargeBinary(), nullable=False),
            sa.Column('meta', sa.JSON(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            _fk('fk_embeddings_user'),
            sa.UniqueConstraint('user_id', 'kind', 'ref_id', name='uq_embeddings_user_kind_ref'),
        ],
        has_owner,
        indexes=[('ix_embeddings_model', ['model']), ('ix_embeddings_user_id', ['user_id'])],
    )
    _rebuild(
        'suggestions',
        [
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('category', sa.String(length=30), nullable=False),
            sa.Column('title', sa.Text(), nullable=False),
            sa.Column('detail', sa.Text(), nullable=False),
            sa.Column('reason', sa.Text(), nullable=False),
            sa.Column('confidence', sa.Float(), nullable=False),
            sa.Column('source', sa.String(length=10), nullable=False),
            sa.Column('proposed', sa.JSON(), nullable=True),
            sa.Column('item_id', sa.Integer(), nullable=True),
            sa.Column('sender', sa.String(length=200), nullable=True),
            sa.Column('url', sa.Text(), nullable=True),
            sa.Column('dedupe_key', sa.String(length=300), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('feedback', sa.String(length=12), nullable=True),
            sa.Column('result', sa.Text(), nullable=True),
            sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            _fk('fk_suggestions_user'),
            sa.UniqueConstraint('user_id', 'dedupe_key', name='uq_suggestions_user_dedupe'),
        ],
        has_owner,
        indexes=[
            ('ix_suggestions_status_created', ['status', 'created_at']),
            ('ix_suggestions_user_id', ['user_id']),
        ],
    )
    _rebuild(
        'plugins',
        [
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=50), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False),
            sa.Column('config', sa.JSON(), nullable=False),
            sa.Column('secrets', sa.Text(), nullable=False),
            sa.Column('webhook_token', sa.String(length=64), nullable=False),
            sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('user_id', 'name'),
            _fk('fk_plugins_user'),
        ],
        has_owner,
    )
    _rebuild(
        'plugin_kv',
        [
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('plugin', sa.String(length=50), nullable=False),
            sa.Column('key', sa.String(length=200), nullable=False),
            sa.Column('value', sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint('user_id', 'plugin', 'key'),
            _fk('fk_plugin_kv_user'),
        ],
        has_owner,
    )


def _rebuild(table: str, columns: list, has_owner: bool, indexes: list | None = None) -> None:
    """Create the new shape under a temporary name, copy the rows across, swap."""
    bind = op.get_bind()
    old_columns = [c['name'] for c in sa.inspect(bind).get_columns(table)]
    temp = f'{table}__new'
    op.create_table(temp, *columns)
    if has_owner:
        names = ', '.join(old_columns)
        bind.execute(
            sa.text(f'INSERT INTO {temp} (user_id, {names}) SELECT {OWNER}, {names} FROM {table}')
        )
    # Constraint and index names must be free before the new table can take them.
    op.drop_table(table)
    op.rename_table(temp, table)
    for name, cols in indexes or []:
        op.create_index(name, table, cols, unique=False)


def downgrade() -> None:
    raise NotImplementedError(
        "multi-user data cannot be folded back into a single-user database; restore a backup"
    )
