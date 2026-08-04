# syntax=docker/dockerfile:1

# Pin the Stoat release as a unit: these three binaries share one Revolt.toml
# schema and must not be upgraded independently.
FROM ghcr.io/stoatchat/api:v0.13.8 AS api
FROM ghcr.io/stoatchat/events:v0.13.8 AS events
FROM ghcr.io/stoatchat/file-server:v0.13.8 AS files
FROM ghcr.io/stoatchat/for-web:746bee5 AS web
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
COPY --from=web /app/dist/ /usr/share/nginx/html/
COPY --from=minio /usr/bin/minio /usr/local/bin/minio
COPY --from=minio_client /usr/bin/mc /usr/local/bin/mc

COPY nginx.conf /etc/nginx/nginx.conf
COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY entrypoint.sh /entrypoint.sh
COPY owner_auth.py /opt/stoat/owner_auth.py
COPY import_stoatbridge.py import_discord_messages.py /opt/stoat/tools/
COPY openhost-sso.js /usr/share/nginx/html/openhost-sso.js

RUN chmod 0755 /entrypoint.sh /opt/stoat/bin/* /opt/stoat/tools/*.py /usr/local/bin/minio /usr/local/bin/mc \
    # Gate the app module behind the owner-session bootstrap. Starting both in
    # parallel lets Stoat hydrate IndexedDB while the SSO record is mid-write.
    && sed -i '/<script type="module" crossorigin src="\/assets\/index-NqnvUoWC.js"><\/script>/d' /usr/share/nginx/html/index.html \
    && sed -i 's#</head>#<script type="module" src="/openhost-sso.js"></script></head>#' /usr/share/nginx/html/index.html \
    && nginx -t

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
