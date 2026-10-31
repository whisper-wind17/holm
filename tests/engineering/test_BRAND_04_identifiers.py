"""BRAND-04: installation identifier guard with a failing CLI positive control."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _assert_healthcheck_chmod_order(dockerfile: str) -> None:
    healthcheck = dockerfile.index("chmod 0555 /opt/holm/bin/holm-healthcheck")
    for bulk in (
        "find /opt/holm -type d -exec chmod 0555 {} +",
        "find /opt/holm -type f -exec chmod 0444 {} +",
    ):
        assert dockerfile.index(bulk) < healthcheck, "healthcheck chmod must follow bulk chmod"


def test_BRAND_04_healthcheck_chmod_order() -> None:
    dockerfile = (ROOT / "deploy/images/application.Dockerfile").read_text()
    _assert_healthcheck_chmod_order(dockerfile)


def test_BRAND_04_healthcheck_chmod_order_positive_control() -> None:
    # FX-BRAND-04-CHMOD: restore the old order that removed the executable bit.
    dockerfile = (ROOT / "deploy/images/application.Dockerfile").read_text()
    corrected = (
        "RUN find /opt/holm -type d -exec chmod 0555 {} + \\\n"
        "    && find /opt/holm -type f -exec chmod 0444 {} + \\\n"
        "    && chmod 0555 /opt/holm/bin/holm-healthcheck"
    )
    broken = (
        "RUN chmod 0555 /opt/holm/bin/holm-healthcheck \\\n"
        "    && find /opt/holm -type d -exec chmod 0555 {} + \\\n"
        "    && find /opt/holm -type f -exec chmod 0444 {} +"
    )
    assert corrected in dockerfile
    with pytest.raises(AssertionError, match="healthcheck chmod must follow bulk chmod"):
        _assert_healthcheck_chmod_order(dockerfile.replace(corrected, broken))
