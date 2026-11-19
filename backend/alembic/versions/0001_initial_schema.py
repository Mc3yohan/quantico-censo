"""Initial multi-tenant Quantico Censo schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from app.db import models  # noqa: F401
from app.db.session import Base

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "postgresql":
        bind.execute(text("CREATE INDEX IF NOT EXISTS ix_territories_boundary_gist ON territories USING GIST (boundary_geom)"))
        bind.execute(text("CREATE INDEX IF NOT EXISTS ix_outlets_location_gist ON outlets USING GIST (location_geom)"))
        bind.execute(text("CREATE INDEX IF NOT EXISTS ix_field_audits_location_gist ON field_audits USING GIST (location_geom)"))


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
