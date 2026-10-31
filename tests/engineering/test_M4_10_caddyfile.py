"""M4-10 production Edge template keeps the TDD 9.9 directives (text-level only).

No Caddy binary exists in the development container (deviation D1/D14), so
these tests pin the directives that carry the routing, streaming, cache, body
and logging rules; running Caddy is left to the deployment card and G1.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
TEMPLATE: Final = REPOSITORY_ROOT / "deploy" / "caddy" / "Caddyfile.tmpl"


def _directives() -> str:
    """Template text without comments, so a comment cannot satisfy an assertion."""
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    return "\n".join(line.split("#", 1)[0].rstrip() for line in lines)


def _block(text: str, opener: str) -> str:
    """Return the brace-balanced block that starts at ``opener``."""
    # Mask {{PLACEHOLDER}} braces with same-length text so offsets still match.
    masked = re.sub(r"\{\{([A-Z_]+)\}\}", lambda found: f"  {found.group(1)}  ", text)
    start = text.index(opener)
    depth = 0
    for index in range(start + len(opener) - 1, len(masked)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated block: {opener}")


def test_M4_10_caddyfile_routes_api_and_v1_to_streaming_proxies() -> None:
    text = _directives()
    assert re.search(r"@control path /api /api/\*\s*$", text, re.MULTILINE)
    assert re.search(r"@gateway path /v1 /v1/\*\s*$", text, re.MULTILINE)
    for matcher, upstream in (("@control", "CONTROL_UPSTREAM"), ("@gateway", "GATEWAY_UPSTREAM")):
        block = _block(text, f"handle {matcher} {{")
        assert f"reverse_proxy {{{{{upstream}}}}}" in block
        assert re.search(r"flush_interval -1\s*$", block, re.MULTILINE)
        assert 'header ?Cache-Control "no-store"' in block
        # SSE must never be compressed, and nothing here may fall back to the SPA.
        assert "encode" not in block
        assert "try_files" not in block
        assert "file_server" not in block


def test_M4_10_caddyfile_spa_fallback_cache_and_assets() -> None:
    text = _directives()
    spa = _block(text, "handle @spa_method {")
    assert re.search(r"@spa_method method GET HEAD\s*$", text, re.MULTILINE)
    assert "try_files {path} /index.html" in spa
    assert 'header Cache-Control "no-store"' in spa
    assets = _block(text, "handle /assets/* {")
    assert 'header @hashed Cache-Control "public, max-age=31536000, immutable"' in assets
    assert "try_files" not in assets
    # The API handles come first so the SPA handle never sees /api or /v1.
    assert text.index("handle @control") < text.index("handle @spa_method")
    assert text.index("handle @gateway") < text.index("handle @spa_method")
    assert text.count("try_files") == 1


def test_M4_10_caddyfile_body_limit_logging_and_tls_placeholder() -> None:
    text = _directives()
    body = _block(text, "request_body {")
    assert re.search(r"max_size 16MiB\s*$", body, re.MULTILINE)
    log = _block(text, "log {")
    for field in ("request>headers delete", "request>uri delete", "resp_headers delete"):
        assert field in log
    assert "{{TLS_DIRECTIVE}}" in text
    assert "tls internal" not in text
