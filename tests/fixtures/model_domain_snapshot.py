"""Shared model-domain write detector established by ``TC-RES-02``."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.engine import Engine


def model_domain_snapshot(engine: Engine, table_names: Sequence[str]) -> str:
    """Capture row values and ``xmin`` so value-preserving rewrites remain visible."""

    assert table_names
    parts: list[str] = []
    with engine.connect() as connection:
        for table_name in table_names:
            assert table_name.isidentifier()
            parts.append(
                str(
                    connection.exec_driver_sql(
                        "SELECT coalesce(json_agg(json_build_object("
                        "'row', row_to_json(scanned), 'xmin', scanned.xmin::text) "
                        f"ORDER BY scanned.id)::text, '[]') FROM core.{table_name} AS scanned"
                    ).scalar_one()
                )
            )
    return "\n".join(parts)


def assert_no_model_domain_writes(before: str, after: str) -> None:
    """The single judgement shared by model-domain read-only cases and controls."""

    assert after == before, "a read-only operation wrote to the model domain"


__all__ = ["assert_no_model_domain_writes", "model_domain_snapshot"]
