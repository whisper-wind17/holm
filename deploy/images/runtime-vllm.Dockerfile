ARG HOLM_BASE_REGISTRY=docker.io
FROM ${HOLM_BASE_REGISTRY}/vllm/vllm-openai:v0.29.0@sha256:c2914767605584b6d8f45686b82de173ecc99e781897aa3d0a66dacd72c51ae1

# Read the installer's constant without importing or retaining installer code.
# The build context is the repository; this bind mount is read-only and build-only.
RUN --mount=type=bind,source=deploy/installer/guards.py,target=/tmp/holm-runtime-guards.py \
    set -eu; \
    if getent passwd 10008 >/dev/null || getent group 10008 >/dev/null; then \
        echo 'Runtime image: upstream UID/GID 10008 is occupied; refusing to renumber' >&2; exit 1; \
    fi; \
    groupadd --gid 10008 runtime; \
    useradd --uid 10008 --gid 10008 --no-create-home --home-dir /var/cache/holm runtime; \
    mkdir -p /var/cache/holm; \
    chown 10008:10008 /var/cache/holm; \
    runtime_bin=$(python3 -c 'import ast; tree = ast.parse(open("/tmp/holm-runtime-guards.py").read()); print(ast.literal_eval(next(node.value for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "RUNTIME_EXECUTABLE_DIRECTORY" for target in node.targets))))'); \
    executable=$(command -v vllm) || { echo 'Runtime image: upstream vllm command missing' >&2; exit 1; }; \
    test -x "$executable"; \
    mkdir -p "$runtime_bin"; \
    ln -s "$executable" "$runtime_bin/vllm"

ENV HOME="/var/cache/holm" \
    HF_HOME="/var/cache/holm/huggingface" \
    XDG_CACHE_HOME="/var/cache/holm"
ENTRYPOINT []
CMD []
USER 10008:10008
