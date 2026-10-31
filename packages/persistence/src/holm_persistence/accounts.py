"""Account, browser-session, and persisted login-protection tables.

These are the account tables. What is worth reading closely are
the check constraints, because they are the part that keeps a bug in some future
call site from turning into a plaintext leak:

* ``ck_users_password_hash_argon2id`` -- the column physically cannot hold
    anything that is not a PHC-encoded Argon2id hash, so "someone stored the
    password" is a failed write, not a silent disaster discovered later.
* ``ck_sessions_session_hash_sha256`` / ``ck_sessions_csrf_hash_sha256`` --
    64 lowercase hex characters. A session token is URL-safe base64 and contains
    characters outside that set with overwhelming probability, so the constraint
    rejects a plaintext cookie value on the way in. This is the database half of
    the rule "不保存 Cookie 明文"; the application half is that no repository method
    below accepts a plaintext token at all.
* ``ck_users_deleted_at_matches_status`` -- ``Deleted`` is a
    logical deletion, so the status and the timestamp are one fact stored twice
    and the database keeps them agreeing.

The enum values are stored as text with a check constraint rather than as a
PostgreSQL ``ENUM`` type. A native enum type would need its own create/drop in
every migration that touches it and its own ``ALTER TYPE`` dance to add a value;
a check constraint is a plain DDL statement that downgrades cleanly, which is
required for an empty database after rollback.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

import sqlalchemy as sa
from holm_common.usernames import DISPLAY_NAME_MAX_LENGTH, USERNAME_MAX_LENGTH
from holm_domain import UserRole, UserStatus

from holm_persistence.schemas import CORE_SCHEMA

#: Deterministic constraint and index names. Alembic and the reflection test
#: both depend on these being stable, and PostgreSQL's own default names are not
#: expressive enough to tell one check constraint from another in an error.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

METADATA: Final[sa.MetaData] = sa.MetaData(naming_convention=NAMING_CONVENTION)

USERS_TABLE_NAME: Final[str] = "users"
SESSIONS_TABLE_NAME: Final[str] = "sessions"
LOGIN_PROTECTION_TABLE_NAME: Final[str] = "login_protection"

#: Repeated in the check constraints below; ``holm_security.tokens``
#: publishes the same shape from the other side of the boundary.
TOKEN_HASH_SQL_PATTERN: Final[str] = "^[0-9a-f]{64}$"
ARGON2ID_HASH_SQL_PATTERN: Final[str] = r"^\$argon2id\$"


def _values_clause(column: str, enumeration: type[StrEnum]) -> str:
    rendered = ", ".join(f"'{member.value}'" for member in enumeration)
    return f"{column} IN ({rendered})"


USERS: Final[sa.Table] = sa.Table(
    USERS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("username_norm", sa.Text(), nullable=False),
    sa.Column("display_name", sa.Text(), nullable=False),
    sa.Column("role", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("password_hash", sa.Text(), nullable=False),
    sa.Column("must_change_password", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint("username_norm"),
    sa.CheckConstraint(_values_clause("role", UserRole), name="role"),
    sa.CheckConstraint(_values_clause("status", UserStatus), name="status"),
    sa.CheckConstraint(
        f"char_length(username_norm) BETWEEN 1 AND {USERNAME_MAX_LENGTH}",
        name="username_norm_length",
    ),
    sa.CheckConstraint(
        f"char_length(display_name) BETWEEN 1 AND {DISPLAY_NAME_MAX_LENGTH}",
        name="display_name_length",
    ),
    sa.CheckConstraint(
        f"password_hash ~ '{ARGON2ID_HASH_SQL_PATTERN}'",
        name="password_hash_argon2id",
    ),
    sa.CheckConstraint(
        f"(status = '{UserStatus.DELETED.value}') = (deleted_at IS NOT NULL)",
        name="deleted_at_matches_status",
    ),
    schema=CORE_SCHEMA,
)

SESSIONS: Final[sa.Table] = sa.Table(
    SESSIONS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column(
        "user_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{USERS_TABLE_NAME}.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    sa.Column("session_hash", sa.Text(), nullable=False),
    sa.Column("csrf_hash", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint("session_hash"),
    sa.CheckConstraint(
        f"session_hash ~ '{TOKEN_HASH_SQL_PATTERN}'",
        name="session_hash_sha256",
    ),
    sa.CheckConstraint(
        f"csrf_hash ~ '{TOKEN_HASH_SQL_PATTERN}'",
        name="csrf_hash_sha256",
    ),
    sa.CheckConstraint("expires_at > created_at", name="expires_after_created"),
    sa.CheckConstraint("last_seen_at >= created_at", name="last_seen_after_created"),
    sa.CheckConstraint(
        "revoked_at IS NULL OR revoked_at >= created_at",
        name="revoked_after_created",
    ),
    schema=CORE_SCHEMA,
)

LOGIN_PROTECTION: Final[sa.Table] = sa.Table(
    LOGIN_PROTECTION_TABLE_NAME,
    METADATA,
    # This is the SHA-256 digest of the normalized submitted identifier, not a
    # User foreign key.  Unknown identifiers need the same persisted lockout as
    # real accounts or the lockout behavior itself becomes an enumeration oracle.
    sa.Column("principal_hash", sa.Text(), primary_key=True),
    sa.Column("failed_attempts", sa.Integer(), nullable=False),
    sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.CheckConstraint(
        f"principal_hash ~ '{TOKEN_HASH_SQL_PATTERN}'",
        name="principal_hash_sha256",
    ),
    sa.CheckConstraint("failed_attempts >= 0", name="failed_attempts_nonnegative"),
    sa.CheckConstraint(
        "(failed_attempts < 5) = (locked_until IS NULL)",
        name="lock_matches_threshold",
    ),
    sa.CheckConstraint(
        "locked_until IS NULL OR locked_until > updated_at",
        name="lock_after_update",
    ),
    schema=CORE_SCHEMA,
)

ACCOUNT_TABLES: Final[tuple[sa.Table, ...]] = (USERS, SESSIONS, LOGIN_PROTECTION)
