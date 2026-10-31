"""Shared, ``bytea``-aware database sentinel scanner for the credential suites.

Promoted verbatim out of ``test_TC_SEC_19_credential_rotation.py`` for M2-03:
the scanner already had two cross-module consumers importing an
underscore-private name, and M2-03's ``TC-RES-10`` is the third.  Only the
definition site and the imports moved -- the scanner body and every assertion
in its consumers are unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine


def database_bytes(engine: Engine) -> bytes:
    """Scan text/JSON plus decoded bytea values across all three application schemas."""

    chunks: list[bytes] = []
    with engine.connect() as connection:
        tables = tuple(
            connection.execute(
                sa.text(
                    """
                    SELECT table_schema, table_name
                    FROM information_schema.tables
                    WHERE table_type = 'BASE TABLE'
                      AND table_schema IN ('core', 'usage', 'telemetry')
                    ORDER BY table_schema, table_name
                    """
                )
            ).tuples()
        )
        assert tables, "credential scan discovered no application tables"
        for schema_name, table_name in tables:
            assert isinstance(schema_name, str) and isinstance(table_name, str)
            preparer = connection.dialect.identifier_preparer
            qualified = f"{preparer.quote(schema_name)}.{preparer.quote(table_name)}"
            chunks.extend(
                str(row).encode("utf-8")
                for row in connection.exec_driver_sql(
                    f"SELECT row_to_json(scanned_row)::text FROM {qualified} AS scanned_row"
                ).scalars()
            )
            bytea_columns = tuple(
                connection.execute(
                    sa.text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = :schema_name
                          AND table_name = :table_name
                          AND data_type = 'bytea'
                        ORDER BY ordinal_position
                        """
                    ),
                    {"schema_name": schema_name, "table_name": table_name},
                ).scalars()
            )
            for column_name in bytea_columns:
                assert isinstance(column_name, str)
                quoted_column = preparer.quote(column_name)
                chunks.extend(
                    bytes(value)
                    for value in connection.exec_driver_sql(
                        f"SELECT {quoted_column} FROM {qualified} WHERE {quoted_column} IS NOT NULL"
                    ).scalars()
                )
    return b"\n".join(chunks)


__all__ = ["database_bytes"]
