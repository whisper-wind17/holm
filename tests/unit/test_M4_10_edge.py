"""Error sanitization assertion and its positive controls."""

import json

import httpx
import pytest


def _assert_sanitized(response: httpx.Response, status: int, code: str) -> None:
    from uuid import UUID

    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "details", "request_id"}
    assert payload["error"]["code"] == code
    assert response.headers["x-request-id"] == payload["error"]["request_id"]
    request_id = payload["error"]["request_id"]
    assert isinstance(request_id, str)
    assert str(UUID(request_id)) == request_id
    # UUID digits can coincide with ports; exclude only this validated field's value.
    payload["error"]["request_id"] = ""
    sanitized_text = json.dumps(payload)
    for leaked in ("127.0.0.1", "18200", "18201", "refused", "Errno", "Traceback", "httpx"):
        assert leaked not in sanitized_text


@pytest.fixture
def fix_40_response() -> httpx.Response:
    # FX-FIX-40: synthetic UUIDv7 reproducing FIX-38's observed time prefix.
    request_id = "01a18201-0000-7000-8000-000000000000"
    return httpx.Response(
        503,
        headers={"cache-control": "no-store", "x-request-id": request_id},
        json={
            "error": {
                "code": "backend_unavailable",
                "message": "Backend unavailable",
                "details": {},
                "request_id": request_id,
            }
        },
    )


def test_FIX_40_uuid_port_substring_is_not_a_leak(fix_40_response: httpx.Response) -> None:
    assert "18201" in fix_40_response.json()["error"]["request_id"]
    _assert_sanitized(fix_40_response, 503, "backend_unavailable")


@pytest.mark.parametrize(
    "leaked", ("127.0.0.1", "18200", "18201", "refused", "Errno", "Traceback", "httpx")
)
@pytest.mark.parametrize("field", ("message", "code", "details_value", "details_key"))
def test_FIX_40_leaks_outside_request_id_fail(
    fix_40_response: httpx.Response, field: str, leaked: str
) -> None:
    payload = fix_40_response.json()
    if field == "details_value":
        payload["error"]["details"] = {"request_id": leaked}
    elif field == "details_key":
        payload["error"]["details"] = {leaked: "value"}
    else:
        payload["error"][field] = leaked
    response = httpx.Response(503, headers=fix_40_response.headers, json=payload)
    with pytest.raises(AssertionError):
        _assert_sanitized(response, 503, payload["error"]["code"])


@pytest.mark.parametrize(
    "request_id",
    (
        "01A18201-0000-7000-8000-000000000000",
        "01a18201000070008000000000000000",
        "{01a18201-0000-7000-8000-000000000000}",
        "127.0.0.1:18201 refused",
    ),
)
def test_FIX_40_noncanonical_request_id_fails(
    fix_40_response: httpx.Response, request_id: str
) -> None:
    payload = fix_40_response.json()
    payload["error"]["request_id"] = request_id
    headers = dict(fix_40_response.headers)
    headers["x-request-id"] = request_id
    response = httpx.Response(503, headers=headers, json=payload)
    with pytest.raises((AssertionError, ValueError)):
        _assert_sanitized(response, 503, "backend_unavailable")
