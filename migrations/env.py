"""Alembic runtime configured with an in-memory SQLAlchemy connection."""

from alembic import context
from holm_persistence.identifier_rename import rename_legacy_identifiers
from sqlalchemy.engine import Connection

target_metadata = None


def run_migrations_offline() -> None:
    """Reject URL-based offline execution because credentials are not stored in config."""
    raise RuntimeError("offline migrations are not configured; provide a SQLAlchemy Connection")


def run_migrations_online() -> None:
    """Run migrations on the connection supplied by the trusted upgrade caller."""
    connection = context.config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise RuntimeError(
            'Alembic requires a SQLAlchemy Connection in Config.attributes["connection"]'
        )

    remove_version_table = context.config.attributes.get("remove_version_table_on_base", False)
    if not isinstance(remove_version_table, bool):
        raise RuntimeError("remove_version_table_on_base must be a boolean")

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transactional_ddl=True,
    )

    with context.begin_transaction():
        # Historical revisions already use Holm names, including their role grants.
        rename_legacy_identifiers(connection)
        context.run_migrations()
        if remove_version_table:
            if context.get_revision_argument() is not None:
                raise RuntimeError("version table cleanup is allowed only when targeting base")
            connection.exec_driver_sql("DROP TABLE public.alembic_version")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
