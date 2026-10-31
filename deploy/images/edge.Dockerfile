ARG HOLM_BASE_REGISTRY=docker.io
FROM ${HOLM_BASE_REGISTRY}/library/caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d

# Edge listens on unprivileged ports; file capabilities conflict with cap_drop ALL.
RUN setcap -r /usr/bin/caddy

RUN addgroup -g 10001 holm \
    && adduser -D -H -u 10001 -G holm holm

COPY build/m0-02/web/ /srv/holm/
COPY deploy/caddy/Caddyfile.tmpl /etc/holm/Caddyfile.tmpl

RUN chmod -R a=rX /srv/holm /etc/holm

USER 10001:10001
