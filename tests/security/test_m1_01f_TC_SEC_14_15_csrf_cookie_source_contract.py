"""TC-SEC-14/15: fail-loud source contracts for the M1-01F security boundary."""

from __future__ import annotations

import ast
import textwrap
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUTH_ROUTES = ROOT / "apps/control_api/src/holm_control_api/auth_routes.py"
AUTHENTICATION = ROOT / "apps/control_api/src/holm_control_api/authentication.py"
BOOTSTRAP_ROUTES = ROOT / "apps/control_api/src/holm_control_api/bootstrap_routes.py"
HTTP_CONSTANTS = ROOT / "apps/control_api/src/holm_control_api/http_constants.py"


class _CallCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self._functions: list[str] = []
        self.calls: list[tuple[str, ast.Call]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if self._functions:
            self.calls.append((self._functions[-1], node))
        self.generic_visit(node)


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _keywords(call: ast.Call) -> dict[str, ast.expr]:
    return {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg is not None}


def _source(expression: ast.AST) -> str:
    return ast.unparse(expression)


def _calls(source: str) -> list[tuple[str, ast.Call]]:
    collector = _CallCollector()
    collector.visit(ast.parse(source))
    return collector.calls


def _replace_once(source: str, old: str, new: str) -> str:
    assert source.count(old) == 1, old
    return source.replace(old, new, 1)


def _assert_auth_routes_contract(source: str) -> None:
    calls = _calls(source)
    cookie_calls: dict[str, list[ast.Call]] = defaultdict(list)
    for function, call in calls:
        if _call_name(call) == "set_cookie":
            cookie_calls[function].append(call)

    assert set(cookie_calls) == {"set_session_cookies", "expire_session_cookies"}
    assert all(len(group) == 2 for group in cookie_calls.values())

    by_function_and_key: dict[str, dict[str, ast.Call]] = {}
    for function, group in cookie_calls.items():
        keyed = {_source(_keywords(call)["key"]): call for call in group}
        assert set(keyed) == {"SESSION_COOKIE_NAME", "CSRF_COOKIE_NAME"}
        by_function_and_key[function] = keyed

    issued_csrf = _keywords(by_function_and_key["set_session_cookies"]["CSRF_COOKIE_NAME"])
    assert _source(issued_csrf["value"]) == "issued.token_pair.csrf_token"
    assert _source(issued_csrf["max_age"]) == ("int(SESSION_ABSOLUTE_TIMEOUT.total_seconds())")
    assert _source(issued_csrf["secure"]) == "True"
    assert _source(issued_csrf["httponly"]) == "False"
    assert _source(issued_csrf["samesite"]) == "'strict'"
    assert _source(issued_csrf["path"]) == "'/'"
    assert "domain" not in issued_csrf

    expired_csrf = _keywords(by_function_and_key["expire_session_cookies"]["CSRF_COOKIE_NAME"])
    expected_expiry = {
        "value": "''",
        "max_age": "0",
        "expires": "0",
        "secure": "True",
        "httponly": "False",
        "samesite": "'strict'",
        "path": "'/'",
    }
    assert {name: _source(expired_csrf[name]) for name in expected_expiry} == expected_expiry
    assert "domain" not in expired_csrf

    issued_pair_calls = [
        call
        for function, call in calls
        if function == "issued_session_response" and _call_name(call) == "set_session_cookies"
    ]
    assert len(issued_pair_calls) == 1
    assert [_source(argument) for argument in issued_pair_calls[0].args] == ["response", "issued"]

    expiry_calls = [
        (function, call) for function, call in calls if _call_name(call) == "expire_session_cookies"
    ]
    assert [
        (function, [_source(argument) for argument in call.args]) for function, call in expiry_calls
    ] == [("logout", ["response"])]

    csrf_inputs = [
        _source(keyword.value)
        for function, call in calls
        if function in {"logout", "change_password"}
        for keyword in call.keywords
        if keyword.arg == "csrf_token"
    ]
    assert csrf_inputs == [
        "request.headers.get(CSRF_HEADER_NAME)",
        "request.headers.get(CSRF_HEADER_NAME)",
    ]
    assert "request.cookies.get(CSRF_COOKIE_NAME)" not in source

    issued_function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "issued_session_response"
    )
    csrf_body_values = [
        value
        for node in ast.walk(issued_function)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == "csrf_token"
    ]
    assert [_source(value) for value in csrf_body_values] == ["issued.token_pair.csrf_token"]


#: M1-10A moved session minting into one private method so login, rotation and
#: the first-administrator bootstrap cannot drift apart. The guard follows the
#: code and got stricter with it: the mint site is asserted the same way it
#: always was, and the delegating callers are now an exact set, so a fourth way
#: to create a session is a failure rather than an unnoticed second path.
_SESSION_MINTING_FUNCTION = "_create_session"
_EXPECTED_MINTING_CALLERS = frozenset(
    {"login", "_rotate_current_session", "issue_session_in_transaction"}
)


def _assert_rotation_contract(source: str) -> None:
    calls = _calls(source)
    create_calls = [
        call
        for function, call in calls
        if function == _SESSION_MINTING_FUNCTION and _call_name(call) == "create"
    ]
    assert len(create_calls) == 1
    assert _source(_keywords(create_calls[0])["csrf_hash"]) == "pair.csrf_hash"

    # No second place may insert a session row.
    row_writes = [
        (function, call)
        for function, call in calls
        if _call_name(call) == "create" and "user_id" in _keywords(call)
    ]
    assert [function for function, _ in row_writes] == [_SESSION_MINTING_FUNCTION]

    delegating = {
        function for function, call in calls if _call_name(call) == _SESSION_MINTING_FUNCTION
    }
    assert delegating == _EXPECTED_MINTING_CALLERS


def _assert_cookie_name_contract(source: str) -> None:
    tree = ast.parse(source)
    assignments = {
        node.target.id: _source(node.value)
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    }
    assert assignments["CSRF_COOKIE_NAME"] == "'__Host-pws_csrf'"


def _assert_bootstrap_routes_reuse_the_cookie_writer(source: str) -> None:
    """M1-10A hands out a session too; it must not grow a second cookie writer."""
    calls = _calls(source)
    assert [call for _, call in calls if _call_name(call) == "set_cookie"] == []
    reused = [function for function, call in calls if _call_name(call) == "issued_session_response"]
    assert reused == ["_created_response"]


def test_TC_SEC_14_m1_01f_source_contract_covers_every_cookie_writer() -> None:
    _assert_auth_routes_contract(AUTH_ROUTES.read_text(encoding="utf-8"))
    _assert_cookie_name_contract(HTTP_CONSTANTS.read_text(encoding="utf-8"))
    _assert_bootstrap_routes_reuse_the_cookie_writer(BOOTSTRAP_ROUTES.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "    return issued_session_response(",
            "    response = json_response(content={}, request_id=request_id)\n"
            "    response.set_cookie(key='x', value='y')\n"
            "    return issued_session_response(",
        ),
    ],
)
def test_M1_10A_TC_SEC_14_bootstrap_second_cookie_writer_positive_control(
    old: str, new: str
) -> None:
    """A second cookie writer inside the bootstrap boundary must fail loudly."""
    source = BOOTSTRAP_ROUTES.read_text(encoding="utf-8")
    changed = _replace_once(source, old, new)
    with pytest.raises(AssertionError):
        _assert_bootstrap_routes_reuse_the_cookie_writer(changed)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "key=CSRF_COOKIE_NAME,\n        value=issued.token_pair.csrf_token,",
            "key=SESSION_COOKIE_NAME,\n        value=issued.token_pair.csrf_token,",
        ),
        (
            'key=CSRF_COOKIE_NAME,\n        value="",',
            'key=SESSION_COOKIE_NAME,\n        value="",',
        ),
        (
            "# header; server-side validation still uses the Session-bound hash.\n"
            "        httponly=False,",
            "# header; server-side validation still uses the Session-bound hash.\n"
            "        httponly=True,",
        ),
        (
            '"csrf_token": issued.token_pair.csrf_token,',
            '"csrf_token": issued.token_pair.session_token,',
        ),
    ],
)
def test_TC_SEC_14_m1_01f_source_guard_positive_controls(old: str, new: str) -> None:
    source = AUTH_ROUTES.read_text(encoding="utf-8")
    changed = _replace_once(source, old, new)
    with pytest.raises((AssertionError, KeyError)):
        _assert_auth_routes_contract(changed)


def test_TC_SEC_14_m1_01f_cookie_fallback_positive_control_fails() -> None:
    source = AUTH_ROUTES.read_text(encoding="utf-8")
    changed = source.replace(
        "csrf_token=request.headers.get(CSRF_HEADER_NAME),",
        "csrf_token=request.cookies.get(CSRF_COOKIE_NAME),",
    )
    assert changed != source
    with pytest.raises(AssertionError):
        _assert_auth_routes_contract(changed)


def test_TC_SEC_15_m1_01f_reused_csrf_hash_positive_control_fails() -> None:
    source = AUTHENTICATION.read_text(encoding="utf-8")
    _assert_rotation_contract(source)
    rotation = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == _SESSION_MINTING_FUNCTION
    )
    rotation_source = textwrap.dedent(ast.get_source_segment(source, rotation) or "")
    changed = _replace_once(
        rotation_source, "csrf_hash=pair.csrf_hash,", "csrf_hash=current.csrf_hash,"
    )
    with pytest.raises(AssertionError):
        _assert_rotation_contract(changed)
