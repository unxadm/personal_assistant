"""oidc identities

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-18 09:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'oidc_identities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('issuer', sa.String(length=300), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('issuer', 'subject', name='uq_oidc_issuer_subject'),
    )
    with op.batch_alter_table('oidc_identities', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_oidc_identities_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_table('oidc_identities')
