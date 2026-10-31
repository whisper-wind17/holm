"""Session ownership for the M5-13 real PostgreSQL fault stack."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fault import harness


@pytest.fixture(scope="session", autouse=True)
def _m5_13_postgres_cluster() -> Iterator[None]:
    """Reuse FX-DB-M0-05 once; individual stacks still reset all business rows."""

    harness.initialize_postgres()
    try:
        yield
    finally:
        harness.shutdown_postgres()
