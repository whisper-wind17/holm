"""Tables for the one-time initialization Token and the initialization fact.

Two invariants live in the DDL rather than in application code, because both
have to survive a concurrent second caller:

*   ``core.bootstrap_tokens.consumed_at`` -- the column exists so "已用" is a
    stored fact.  The one-time property itself is not a column: it is the
    single conditional ``UPDATE ... WHERE consumed_at IS NULL`` in
    ``BootstrapTokenRepository.consume``.  Two transactions running that
    statement serialize on the row lock and the loser re-evaluates the
    predicate, so exactly one of them can observe a returned row.
*   ``core.system_initialization`` -- a singleton table.  ``id`` is a primary
    key constrained to one literal value, so a second initialization is a
    unique violation rather than an application-level "did somebody already
    do this?" read.  A trigger rejects ``UPDATE`` and ``DELETE`` so the
    recorded instant is immutable at the database layer too.

Only digests reach this module: ``token_hash`` has the same SHA-256 shape
constraint as the session columns, so a plaintext initialization Token is a
failed write rather than a silent leak.
"""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa

from holm_persistence.accounts import METADATA, TOKEN_HASH_SQL_PATTERN, USERS_TABLE_NAME
from holm_persistence.schemas import CORE_SCHEMA

BOOTSTRAP_TOKENS_TABLE_NAME: Final[str] = "bootstrap_tokens"
SYSTEM_INITIALIZATION_TABLE_NAME: Final[str] = "system_initialization"

#: The singleton row identifier. It is a literal rather than a sequence so the
#: primary key itself forbids a second row.
SYSTEM_INITIALIZATION_ROW_ID: Final[int] = 1

#: Name of the trigger function that makes the recorded instant immutable.
SYSTEM_INITIALIZATION_GUARD_FUNCTION: Final[str] = "reject_system_initialization_mutation"
SYSTEM_INITIALIZATION_GUARD_TRIGGER: Final[str] = "trg_system_initialization_immutable"

BOOTSTRAP_TOKENS: Final[sa.Table] = sa.Table(
    BOOTSTRAP_TOKENS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("token_hash", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint("token_hash"),
    sa.CheckConstraint(
        f"token_hash ~ '{TOKEN_HASH_SQL_PATTERN}'",
        name="token_hash_sha256",
    ),
    sa.CheckConstraint("expires_at > created_at", name="expires_after_created"),
    sa.CheckConstraint(
        "consumed_at IS NULL OR consumed_at >= created_at",
        name="consumed_after_created",
    ),
    schema=CORE_SCHEMA,
)

SYSTEM_INITIALIZATION: Final[sa.Table] = sa.Table(
    SYSTEM_INITIALIZATION_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.SmallInteger(), primary_key=True, autoincrement=False),
    sa.Column("system_initialized_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "admin_user_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{USERS_TABLE_NAME}.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.CheckConstraint(f"id = {SYSTEM_INITIALIZATION_ROW_ID}", name="singleton_row"),
    schema=CORE_SCHEMA,
)

BOOTSTRAP_TABLES: Final[tuple[sa.Table, ...]] = (BOOTSTRAP_TOKENS, SYSTEM_INITIALIZATION)


__all__ = [
    "BOOTSTRAP_TABLES",
    "BOOTSTRAP_TOKENS",
    "BOOTSTRAP_TOKENS_TABLE_NAME",
    "SYSTEM_INITIALIZATION",
    "SYSTEM_INITIALIZATION_GUARD_FUNCTION",
    "SYSTEM_INITIALIZATION_GUARD_TRIGGER",
    "SYSTEM_INITIALIZATION_ROW_ID",
    "SYSTEM_INITIALIZATION_TABLE_NAME",
]
