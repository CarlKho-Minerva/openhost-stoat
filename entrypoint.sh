#!/bin/sh
set -eu

log() { echo "[openhost-stoat] $*"; }

APP_DATA="${OPENHOST_APP_DATA_DIR:-/data/app_data/stoat}"
APP_ARCHIVE="${OPENHOST_APP_ARCHIVE_DIR:-/data/app_archive/stoat}"
export OPENHOST_APP_DATA_DIR="$APP_DATA"
export OPENHOST_APP_ARCHIVE_DIR="$APP_ARCHIVE"

mkdir -p \
  "$APP_DATA/mongo" \
  "$APP_DATA/redis" \
  "$APP_DATA/rabbitmq" \
  "$APP_ARCHIVE/minio" \
  /run/stoat

# The Debian RabbitMQ launcher drops privileges to the rabbitmq account even
# when supervisord starts it as root. OpenHost creates mounted directories as
# the container user, so repair ownership on every boot (including upgrades
# from the initial image, which left this tree root-owned).
chown -R rabbitmq:rabbitmq "$APP_DATA/rabbitmq"

# Secrets must survive image upgrades. A newly generated encryption key would
# make every previously uploaded file unreadable.
SECRETS_FILE="$APP_DATA/secrets.env"
if [ ! -f "$SECRETS_FILE" ]; then
  log "generating persistent local service secrets"
  umask 077
  {
    printf 'FILES_ENCRYPTION_KEY=%s\n' "$(openssl rand -base64 32 | tr -d '\n')"
    printf 'MINIO_ROOT_USER=stoat%s\n' "$(openssl rand -hex 4)"
    printf 'MINIO_ROOT_PASSWORD=%s\n' "$(openssl rand -hex 24)"
  } > "$SECRETS_FILE"
fi
chmod 0600 "$SECRETS_FILE"

set -a
# shellcheck disable=SC1090
. "$SECRETS_FILE"
set +a

PUBLIC_HOST="${OPENHOST_APP_NAME:-stoat}.${OPENHOST_ZONE_DOMAIN:?OPENHOST_ZONE_DOMAIN is required}"
PUBLIC_ORIGIN="https://$PUBLIC_HOST"

# The official for-web image deliberately ships Vite placeholders for its
# deployment wrapper to replace. Serving them verbatim makes URL construction
# fail as soon as an authenticated session starts connecting.
for asset in /usr/share/nginx/html/assets/index-*.js; do
  sed -i \
    -e "s#__VITE_API_URL__#$PUBLIC_ORIGIN/api#g" \
    -e "s#__VITE_WS_URL__#wss://$PUBLIC_HOST/ws#g" \
    -e "s#__VITE_MEDIA_URL__#$PUBLIC_ORIGIN/autumn#g" \
    -e "s#__VITE_PROXY_URL__#$PUBLIC_ORIGIN/january#g" \
    -e "s#__VITE_GIFBOX_URL__#https://api.gifbox.me#g" \
    -e "s#__VITE_HCAPTCHA_SITEKEY__##g" \
    -e "s#__VITE_RNNOISE_WORKLET_CDN_URL__##g" \
    -e "s#__VITE_CFG_ENABLE_VIDEO__#false#g" \
    "$asset"
done

cat > /Revolt.toml <<EOF
production = true
environment = "production"

[database]
mongodb = "mongodb://127.0.0.1:27017"
redis = "redis://127.0.0.1:6379/"

[hosts]
app = "$PUBLIC_ORIGIN"
api = "$PUBLIC_ORIGIN/api"
events = "wss://$PUBLIC_HOST/ws"
autumn = "$PUBLIC_ORIGIN/autumn"
january = "$PUBLIC_ORIGIN/january"
voso_legacy = ""
voso_legacy_ws = ""

[hosts.livekit]

[rabbit]
host = "127.0.0.1"
port = 5672
username = "guest"
password = "guest"
default_exchange = "revolt.default"

[rabbit.queues]
acks = "internal.ack"

[api.registration]
invite_only = false

[api.smtp]
host = ""
username = ""
password = ""
from_address = "noreply@$PUBLIC_HOST"

[api.security]
trust_cloudflare = false

[api.security.captcha]
hcaptcha_key = ""
hcaptcha_sitekey = ""

[api.livekit.nodes]

[files]
encryption_key = "$FILES_ENCRYPTION_KEY"
blocked_mime_types = []
scan_mime_types = []

[files.s3]
endpoint = "http://127.0.0.1:9000"
path_style_buckets = true
region = "us-east-1"
access_key_id = "$MINIO_ROOT_USER"
secret_access_key = "$MINIO_ROOT_PASSWORD"
default_bucket = "revolt-uploads"

# Stock Revolt caps attachments at 20 MB, which is under what a real Discord
# archive holds - screen recordings and lecture captures run past 100 MB. This
# is a single-owner instance on a 135 GB volume, so the cap buys nothing and
# would silently leave those files out. Partial tables merge over the compiled
# defaults (upstream's generate_config.sh sets video keys the same way), so
# only the raised values need stating.
# The TOML key is file_upload_size_limit - SINGULAR. The API advertises it back
# as "file_upload_size_limits" (plural) in /api/, so copying the name from there
# produces config that parses, deploys, and is silently ignored. Confirmed by
# reading the default config embedded in the revolt-autumn binary, which is the
# only place the real spelling appears. Raised so the Discord archive's screen
# recordings and lecture captures (up to 116 MB) import natively instead of
# being dropped; this is a single-owner instance with 135 GB free.
[features.limits.global]
body_limit_size = 200000000

[features.limits.new_user.file_upload_size_limit]
attachments = 150000000
avatars = 4000000
backgrounds = 6000000
icons = 2500000
banners = 6000000
emojis = 500000

[features.limits.default.file_upload_size_limit]
attachments = 150000000
avatars = 4000000
backgrounds = 6000000
icons = 2500000
banners = 6000000
emojis = 500000
EOF

printf '{"api":"%s/api"}\n' "$PUBLIC_ORIGIN" > /run/stoat/stoat.json
chmod 0644 /run/stoat/stoat.json

# RabbitMQ is loopback-only; keep its mutable state on the persistent volume.
export RABBITMQ_NODE_IP_ADDRESS=127.0.0.1
export RABBITMQ_MNESIA_BASE="$APP_DATA/rabbitmq/mnesia"
export RABBITMQ_LOG_BASE="-"
export RABBITMQ_PID_FILE=/tmp/rabbitmq.pid
export MINIO_ROOT_USER MINIO_ROOT_PASSWORD

log "starting unified Stoat stack for $PUBLIC_HOST"
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
