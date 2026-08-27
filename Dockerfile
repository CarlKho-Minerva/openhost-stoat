# syntax=docker/dockerfile:1

# Pin the Stoat release as a unit: these three binaries share one Revolt.toml
# schema and must not be upgraded independently.
FROM ghcr.io/stoatchat/api:v0.13.8 AS api
FROM ghcr.io/stoatchat/events:v0.13.8 AS events
FROM ghcr.io/stoatchat/file-server:v0.13.8 AS files
FROM ghcr.io/stoatchat/proxy:v0.13.8 AS proxy
# The web client is rebuilt from source rather than pulled as an image, so the
# local patches below are actually in the bundle that ships. It is pinned to
# 746bee5821e5474cbcdeea414d04ead87b85fe43 - the exact commit
# ghcr.io/stoatchat/for-web:746bee5 was built from, released as
# stoat-for-web 0.10.0 on 2026-06-30. Building from upstream main instead would
# drag in 128 unrelated commits and a client two minor versions ahead of the
# v0.13.8 API this image runs, which is a separate decision from adding a
# keyboard shortcut.
FROM node:26-bookworm AS web
ARG FOR_WEB_COMMIT=746bee5821e5474cbcdeea414d04ead87b85fe43
RUN corepack enable
WORKDIR /src
RUN git init -q . \
 && git remote add origin https://github.com/stoatchat/for-web.git \
 && git fetch -q --depth 1 origin "$FOR_WEB_COMMIT" \
 && git checkout -q FETCH_HEAD \
 && git submodule update --init --recursive --depth 1

# ctrl+f message search and the ctrl+k quick switcher. Vendored as patches so
# this repo stays the only thing that has to be pushed to deploy them; a fork
# would be a second repo to keep in sync. git apply fails loudly on drift,
# which is what should happen if the pinned commit ever moves.
COPY patches/for-web/ /patches/
# Applied one at a time and in order: the second patch's context includes lines
# the first one adds, so a single `git apply` --check over both would reject it.
RUN set -e; for patch in /patches/*.patch; do echo "==> applying $patch"; git apply --verbose "$patch"; done

RUN pnpm install --frozen-lockfile \
 && pnpm --filter @lingui-solid/babel-plugin-lingui-macro build \
 && pnpm --filter @lingui-solid/babel-plugin-extract-messages build \
 && pnpm --filter solid-livekit-components build \
 && pnpm --filter stoat.js build \
 && pnpm --filter client exec node scripts/copyAssets.mjs \
 && pnpm --filter client exec lingui compile --typescript

# entrypoint.sh rewrites these placeholders at container start, so the build
# has to emit them literally rather than bake in a hostname. VITE_HOST is
# deliberately absent: entrypoint does not substitute it, so setting it here
# would ship the literal string "__VITE_HOST__" as the default host.
ENV VITE_API_URL=__VITE_API_URL__ \
    VITE_WS_URL=__VITE_WS_URL__ \
    VITE_MEDIA_URL=__VITE_MEDIA_URL__ \
    VITE_PROXY_URL=__VITE_PROXY_URL__ \
    VITE_GIFBOX_URL=__VITE_GIFBOX_URL__ \
    VITE_HCAPTCHA_SITEKEY=__VITE_HCAPTCHA_SITEKEY__ \
    VITE_RNNOISE_WORKLET_CDN_URL=__VITE_RNNOISE_WORKLET_CDN_URL__ \
    VITE_CFG_ENABLE_VIDEO=__VITE_CFG_ENABLE_VIDEO__
RUN pnpm --filter client exec vite build
FROM quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z AS minio
FROM quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z AS minio_client

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    OPENHOST_APP_DATA_DIR=/data/app_data/stoat \
    OPENHOST_APP_ARCHIVE_DIR=/data/app_archive/stoat

# Ubuntu does not ship mongod. Install MongoDB's official Noble package, plus
# the other loopback-only services supervised inside this container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl gnupg netcat-openbsd nginx python3-pymongo rabbitmq-server redis-server supervisor \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://pgp.mongodb.com/server-8.0.asc \
       | gpg --dearmor -o /etc/apt/keyrings/mongodb-server-8.0.gpg \
    && echo "deb [arch=amd64,arm64 signed-by=/etc/apt/keyrings/mongodb-server-8.0.gpg] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" \
       > /etc/apt/sources.list.d/mongodb-org-8.0.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends mongodb-org-server mongodb-mongosh \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/stoat/bin /opt/stoat/tools /usr/share/nginx/html /var/log/supervisor

COPY --from=api /home/nonroot/revolt-delta /opt/stoat/bin/revolt-delta
COPY --from=events /home/nonroot/revolt-bonfire /opt/stoat/bin/revolt-bonfire
COPY --from=files /home/nonroot/revolt-autumn /opt/stoat/bin/revolt-autumn
COPY --from=proxy /home/nonroot/revolt-january /opt/stoat/bin/revolt-january
COPY --from=proxy /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=proxy /usr/local/bin/ffprobe /usr/local/bin/ffprobe
COPY --from=web /src/packages/client/dist/ /usr/share/nginx/html/
COPY --from=minio /usr/bin/minio /usr/local/bin/minio
COPY --from=minio_client /usr/bin/mc /usr/local/bin/mc

COPY nginx.conf /etc/nginx/nginx.conf
COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY entrypoint.sh /entrypoint.sh
COPY owner_auth.py /opt/stoat/owner_auth.py
COPY search_bridge.py /opt/stoat/search_bridge.py
COPY import_stoatbridge.py import_discord_messages.py backfill_message_embeds.py /opt/stoat/tools/
COPY openhost-sso.js /usr/share/nginx/html/openhost-sso.js

RUN chmod 0755 /entrypoint.sh /opt/stoat/bin/* /opt/stoat/tools/*.py /usr/local/bin/minio /usr/local/bin/mc \
    # Gate the app module behind the owner-session bootstrap. Starting both in
    # parallel lets Stoat hydrate IndexedDB while the SSO record is mid-write.
    && sed -i '/<script type="module" crossorigin src="\/assets\/index-NqnvUoWC.js"><\/script>/d' /usr/share/nginx/html/index.html \
    && sed -i 's#</head>#<script type="module" src="/openhost-sso.js"></script></head>#' /usr/share/nginx/html/index.html \
    && nginx -t

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
