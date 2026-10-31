# tools only; source and generated dependencies stay in the output workspace.
ARG HOLM_BASE_REGISTRY=docker.io
FROM ${HOLM_BASE_REGISTRY}/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG APT_MIRROR
ARG APT_SECURITY_MIRROR
RUN python -c "import os, pathlib; p = pathlib.Path('/etc/apt/sources.list.d/debian.sources'); s = p.read_text(); mirrors = {'http://deb.debian.org/debian': 'APT_MIRROR', 'http://deb.debian.org/debian-security': 'APT_SECURITY_MIRROR'}; p.write_text(''.join('URIs: ' + (os.environ.get(mirrors[line[6:].strip()]) or line[6:].strip()) + '\n' if line.startswith('URIs: ') and line[6:].strip() in mirrors else line for line in s.splitlines(keepends=True)))" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Official Node release archive; the checksum is from its SHASUMS256.txt.
RUN python -c "import hashlib, pathlib, urllib.request; p = pathlib.Path('/tmp/node.tar.xz'); urllib.request.urlretrieve('https://nodejs.org/dist/v24.14.0/node-v24.14.0-linux-x64.tar.xz', p); assert hashlib.sha256(p.read_bytes()).hexdigest() == '41cd79bb7877c81605a9e68ec4c91547774f46a40c67a17e34d7179ef11729df'" \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && test "$(node --version)" = v24.14.0 \
    && test "$(npm --version)" = 11.9.0

ARG UV_DEFAULT_INDEX
ARG npm_config_registry
RUN python -m pip install --disable-pip-version-check --no-cache-dir ${UV_DEFAULT_INDEX:+--index-url "$UV_DEFAULT_INDEX"} uv==0.11.7
ENV UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1
