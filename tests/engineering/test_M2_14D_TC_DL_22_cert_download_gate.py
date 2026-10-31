"""M2-14D DoD 1-2: the certification tier is a dev command that CI never reaches.

The hole this closes is "no network, so skip, so green": a certification suite
that CI can collect either breaks CI (no network) or gets skipped into a silent
pass.  Reading ``dev`` once by eye does not pin that; this test reads the real
script, follows every function the ``ci`` arm can reach, and fails if any of them
names the certification suite or runs a pytest whose targets could include it.

Both halves of the guard are pinned: the judge (positive controls mutate the
real ``dev`` text, not a stand-in) and the configuration it judges (the real
script, with a floor on how many pytest invocations it must actually see).
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEV = PROJECT_ROOT / "dev"
CERTIFICATION_SUITE = "tests/certification"

_FUNCTION_START = re.compile(r"^([a-z_][a-z0-9_]*)\(\) \{$")
_CASE_ARM = re.compile(r"^    ([^()\s][^()]*)\)$")
_SHELL_OPERATORS = frozenset({";", "&&", "||", "|", ">", "2>&1"})
_PYTEST_OPTIONS_WITH_VALUES = frozenset({"-k", "-m"})


def _functions(text: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    name: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if name is None:
            match = _FUNCTION_START.match(line)
            if match:
                name, lines = match.group(1), []
        elif line == "}":
            bodies[name] = "\n".join(lines)
            name = None
        else:
            lines.append(line)
    assert name is None, f"unterminated function {name!r} in dev"
    assert "main" in bodies, "dev has no main()"
    return bodies


def _case_arms(main_body: str) -> dict[str, str]:
    arms: dict[str, str] = {}
    label: str | None = None
    lines: list[str] = []
    for line in main_body.splitlines():
        if label is None:
            match = _CASE_ARM.match(line)
            if match:
                label, lines = match.group(1), []
        elif line.strip() == ";;":
            arms[label] = "\n".join(lines)
            label = None
        else:
            lines.append(line)
    return arms


def _arm(arms: dict[str, str], command: str) -> str | None:
    for label, body in arms.items():
        if command in (part.strip() for part in label.split("|")):
            return body
    return None


def _reachable(entry: str, functions: dict[str, str]) -> dict[str, str]:
    seen: dict[str, str] = {}
    pending = [entry]
    while pending:
        body = pending.pop()
        for name, function_body in functions.items():
            if name not in seen and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", body):
                seen[name] = function_body
                pending.append(function_body)
    return seen


def _pytest_invocations(body: str) -> list[list[str]]:
    invocations: list[list[str]] = []
    for logical in body.replace("\\\n", " ").splitlines():
        if "pytest" not in logical or logical.lstrip().startswith("#"):
            continue
        tokens = shlex.split(logical, comments=True)
        if "pytest" not in tokens:
            continue
        arguments: list[str] = []
        for token in tokens[tokens.index("pytest") + 1 :]:
            if token in _SHELL_OPERATORS:
                break
            arguments.append(token)
        invocations.append(arguments)
    return invocations


def _pytest_targets(arguments: list[str]) -> tuple[list[str], str | None]:
    targets: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in _PYTEST_OPTIONS_WITH_VALUES:
            if index + 1 >= len(arguments):
                return targets, f"pytest option {argument!r} has no value"
            index += 2
            continue
        if any(argument.startswith(f"{option}=") for option in _PYTEST_OPTIONS_WITH_VALUES):
            index += 1
            continue
        if not argument.startswith("-"):
            targets.append(argument)
        index += 1
    return targets, None


def ci_certification_violations(text: str) -> tuple[tuple[str, ...], int]:
    """Return (violations, pytest invocations seen) for everything ``ci`` reaches."""

    functions = _functions(text)
    ci_body = _arm(_case_arms(functions["main"]), "ci")
    assert ci_body is not None, "dev main has no ci arm"
    reached = {"<ci arm>": ci_body, **_reachable(ci_body, functions)}
    violations: list[str] = []
    seen = 0
    for name, body in reached.items():
        if CERTIFICATION_SUITE in body or re.search(r"cert[-_]download", body):
            violations.append(f"ci reaches {name}, which references the certification tier")
        for arguments in _pytest_invocations(body):
            seen += 1
            targets, parse_error = _pytest_targets(arguments)
            if parse_error is not None:
                violations.append(f"{name}: {parse_error}")
            if not targets:
                violations.append(f"{name}: pytest without targets collects testpaths=tests")
            for target in targets:
                normalized = target.rstrip("/")
                if not normalized.startswith("tests/"):
                    violations.append(f"{name}: unrecognised pytest target {target!r}")
                elif normalized == CERTIFICATION_SUITE or normalized.startswith(
                    CERTIFICATION_SUITE + "/"
                ):
                    violations.append(f"{name}: pytest targets the certification suite")
    return tuple(violations), seen


def cert_command_violations(text: str) -> tuple[str, ...]:
    functions = _functions(text)
    violations: list[str] = []
    body = _arm(_case_arms(functions["main"]), "cert-download")
    if body is None:
        violations.append("dev main has no cert-download arm")
    else:
        reached = {"<cert-download arm>": body, **_reachable(body, functions)}
        if not any(CERTIFICATION_SUITE in value for value in reached.values()):
            violations.append("cert-download never runs tests/certification")
    if not re.search(r"^  cert-download\s", functions.get("usage", ""), re.MULTILINE):
        violations.append("usage does not list cert-download")
    return tuple(violations)


def _dev_text() -> str:
    return DEV.read_text(encoding="utf-8")


def _mutate(text: str, anchor: str, replacement: str) -> str:
    assert text.count(anchor) == 1, f"anchor must hit exactly once: {anchor!r}"
    return text.replace(anchor, replacement)


def test_M2_14D_TC_DL_22_ci_never_reaches_the_certification_tier() -> None:
    violations, seen = ci_certification_violations(_dev_text())

    assert violations == ()
    # unit_tests and integration_tests: seeing none would make "no violation" vacuous.
    assert seen >= 2, seen


def test_M2_14D_TC_DL_22_cert_download_is_a_listed_dev_command_running_the_suite() -> None:
    assert cert_command_violations(_dev_text()) == ()

    result = subprocess.run([str(DEV), "help"], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    # The public export hides development-only entry points from help.
    if (PROJECT_ROOT / ".holm-public-export").is_file():
        assert "cert-download" not in result.stdout
    else:
        assert "cert-download" in result.stdout


def test_M2_14D_TC_DL_22_certification_suite_exists_outside_every_ci_target() -> None:
    suite = PROJECT_ROOT / CERTIFICATION_SUITE
    assert sorted(path.name for path in suite.glob("test_*.py")), "no certification tests"


def test_M2_14D_TC_DL_22_positive_control_ci_calling_the_suite_directly_fails() -> None:
    mutated = _mutate(
        _dev_text(),
        "ci_gates() {\n  declare_unimplemented_gates\n",
        "ci_gates() {\n  declare_unimplemented_gates\n"
        '  run_step "Certify" uv run --frozen pytest tests/certification\n',
    )

    violations, _ = ci_certification_violations(mutated)
    assert any("certification" in value for value in violations), violations


def test_M2_14D_TC_DL_22_positive_control_marker_value_does_not_hide_certification() -> None:
    # 锚点取 unit 步骤的开头：main 与带 perf 步骤的分支上都只出现一次，
    # 所以这条阳性对照不依赖某一分支的 dev 内容。
    mutated = _mutate(
        _dev_text(),
        "pytest tests/engineering tests/unit",
        'pytest tests/certification -m "not perf" tests/engineering tests/unit',
    )

    violations, _ = ci_certification_violations(mutated)
    assert any("targets the certification suite" in value for value in violations), violations


def test_M2_14D_TC_DL_22_positive_control_ci_reaching_the_suite_transitively_fails() -> None:
    mutated = _mutate(
        _dev_text(),
        "fast_check() {\n",
        "certify_quietly() {\n"
        '  run_step "Certify" uv run --frozen pytest tests/certification/\n'
        "}\n\n"
        "fast_check() {\n  certify_quietly\n",
    )

    violations, _ = ci_certification_violations(mutated)
    assert any("certify_quietly" in value for value in violations), violations


def test_M2_14D_TC_DL_22_positive_control_bare_or_opaque_pytest_targets_fail() -> None:
    bare = _mutate(
        _dev_text(),
        "uv run --frozen pytest tests/integration",
        "uv run --frozen pytest",
    )
    opaque = _mutate(
        _dev_text(),
        "uv run --frozen pytest tests/integration",
        'uv run --frozen pytest "$@"',
    )

    bare_violations, _ = ci_certification_violations(bare)
    opaque_violations, _ = ci_certification_violations(opaque)
    assert any("without targets" in value for value in bare_violations), bare_violations
    assert any("unrecognised" in value for value in opaque_violations), opaque_violations
