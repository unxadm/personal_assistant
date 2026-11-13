"""suggestions, proactive marks, audit log

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13 18:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('suggestions',
    sa.Column('id', sa.Integer(), nullable=False),
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
    sa.UniqueConstraint('dedupe_key')
    )
    with op.batch_alter_table('suggestions', schema=None) as batch_op:
        batch_op.create_index('ix_suggestions_status_created', ['status', 'created_at'], unique=False)

    op.create_table('proactive_marks',
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('item_id')
    )

    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=20), nullable=False),
    sa.Column('action', sa.String(length=80), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('change', sa.Boolean(), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('undo', sa.JSON(), nullable=True),
    sa.Column('undone_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index('ix_audit_log_at', ['at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_log_at')
    op.drop_table('audit_log')
    op.drop_table('proactive_marks')
    with op.batch_alter_table('suggestions', schema=None) as batch_op:
        batch_op.drop_index('ix_suggestions_status_created')
    op.drop_table('suggestions')
