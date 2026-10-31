# all Python platform processes share this immutable application image.
# The build context is the repository root after `./dev build` has produced the
# reviewed wheel set. Dependency installation happens only while building the
# image; no source tree or package manager is present in the runtime command.
ARG HOLM_BASE_REGISTRY=docker.io
FROM ${HOLM_BASE_REGISTRY}/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PATH="/opt/holm/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

RUN for identity in 'migrate:10002:10002' 'control-api:10003:10003' 'spool:10004:10004' 'download-worker:10005:10003' 'instance-controller:10006:10006' 'maintenance-worker:10007:10007'; do \
        name="${identity%%:*}"; uid="${identity#*:}"; uid="${uid%%:*}"; gid="${identity##*:}"; \
        groupadd --gid "$uid" "$name" && useradd --uid "$uid" --gid "$gid" \
            --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$name" || exit 1; \
    done \
    && groupadd --gid 10009 holm-agent

WORKDIR /opt/holm

COPY build/m0-02/python/holm_control_api-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_gateway-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_host_agent-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_download_worker-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_instance_controller-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_ledger_writer-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_maintenance_worker-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_common-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_contracts-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_domain-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_ledger-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_persistence-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_protocols-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_runtimes-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_security-0.1.0rc1-py3-none-any.whl /wheelhouse/
COPY build/m0-02/python/holm_telemetry-0.1.0rc1-py3-none-any.whl /wheelhouse/

RUN python -m pip install --disable-pip-version-check --no-cache-dir /wheelhouse/*.whl \
    && rm -rf /wheelhouse

RUN python -c "import importlib, psycopg, psycopg.pq; tuple(importlib.import_module(name) for name in ('holm_control_api.__main__', 'holm_gateway.__main__', 'holm_download_worker.__main__', 'holm_instance_controller.__main__', 'holm_ledger_writer.__main__', 'holm_maintenance_worker.__main__', 'holm_host_agent.__main__')); assert psycopg.pq.__impl__ in ('python', 'c', 'binary')"

COPY migrations /opt/holm/migrations
COPY runtime-compat.yaml /opt/holm/runtime-compat.yaml
COPY deploy/images/holm_healthcheck.py /opt/holm/bin/holm-healthcheck

RUN find /opt/holm -type d -exec chmod 0555 {} + \
    && find /opt/holm -type f -exec chmod 0444 {} + \
    && chmod 0555 /opt/holm/bin/holm-healthcheck

USER 10002:10002
