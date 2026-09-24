# syntax=docker/dockerfile:1

# Pin the Stoat release as a unit: these three binaries share one Revolt.toml
# schema and must not be upgraded independently.
FROM ghcr.io/stoatchat/api:v0.13.8 AS api
FROM ghcr.io/stoatchat/events:v0.13.8 AS events
FROM ghcr.io/stoatchat/file-server:v0.13.8 AS files
FROM ghcr.io/stoatchat/proxy:v0.13.8 AS proxy
# The web client is stoat-for-web 746bee5 (0.10.0, the commit the upstream
# ghcr.io/stoatchat/for-web:746bee5 image was built from) with patches/for-web
# applied: ctrl+f message search and the ctrl+k quick switcher. It is built by
# .github/workflows/build-web.yml on GitHub Actions and pulled here by digest,
# because this zone cannot build it: `vite build` is OOM-killed on 8 GiB with
# no swap, and OpenHost's memory_mb applies only to the run container.
# The image holds the bundle at /app/dist, the same path as upstream's, so the
# COPY below is unchanged. To ship a patch change: let the Action run, check its
# summary, then bump this digest deliberately.
#
# History:
# - 2026-09-09: built from source on the zone (0fcd934). OOM-killed every time;
#   Stoat was down until 09-15.
# - 2026-09-15: reverted to the prebuilt upstream image, patches kept (ca1dee6).
# - 2026-09-24: built on Actions instead, run 36039493850 from 82aa749.
FROM ghcr.io/carlkho-minerva/stoat-web@sha256:4456660e22dcef8b78a6e4f67bc2f92eaa260735bb4ffbddda7e55aa0f612e6b AS web
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
COPY --from=web /app/dist/ /usr/share/nginx/html/
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
