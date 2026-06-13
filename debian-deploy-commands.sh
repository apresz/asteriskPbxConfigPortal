#!/usr/bin/env bash
#
# Debian deployment command file for the Asterisk PBX Config Portal.
#
# Run this on a fresh Debian 12 server as root, after reviewing the variables
# below. It installs the required host packages/services, copies a locally
# prepared project folder, builds a production Docker image, starts PostgreSQL +
# Gunicorn with Docker Compose, configures Nginx, and creates backup service
# commands.
#
# Required before running:
#   export PORTAL_HOST="pbx-portal.example.internal"
#
# The script will ask for the local source folder path at runtime. The admin
# should prepare and upload the repository folder to this server first.
#
# Optional examples:
#   export SOURCE_DIR="/root/uploads/asterisk-pbx-config-portal"
#   export ALLOWED_CLIENT_CIDRS="10.0.0.0/8,100.64.0.0/10,192.168.0.0/16"
#   export ENABLE_UFW="true"
#   export ENABLE_LETS_ENCRYPT="false"
#
# This script intentionally does not print generated secrets.

set -euo pipefail

###############################################################################
# 1. Deployment variables
###############################################################################

APP_NAME="${APP_NAME:-asterisk-pbx-config-portal}"
APP_DIR="${APP_DIR:-/opt/asterisk-pbx-config-portal}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/asterisk-pbx-config-portal}"

SOURCE_DIR="${SOURCE_DIR:-}"
PORTAL_HOST="${PORTAL_HOST:-pbx-portal.example.internal}"

# Keep production portal access limited to trusted LAN/WARP networks.
ALLOWED_CLIENT_CIDRS="${ALLOWED_CLIENT_CIDRS:-127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,::1/128,fc00::/7,fe80::/10}"
TRUSTED_PROXY_CIDRS="${TRUSTED_PROXY_CIDRS:-127.0.0.0/8,::1/128}"

# Firewall and public certificate automation are optional because many PBX
# portals are LAN/WARP-only and may use internal DNS/certificates.
ENABLE_UFW="${ENABLE_UFW:-false}"
ENABLE_LETS_ENCRYPT="${ENABLE_LETS_ENCRYPT:-false}"
LETS_ENCRYPT_EMAIL="${LETS_ENCRYPT_EMAIL:-admin@example.internal}"

# Set to true only when you want an interactive createsuperuser prompt.
CREATE_SUPERUSER_NOW="${CREATE_SUPERUSER_NOW:-false}"

###############################################################################
# 2. Root and variable checks
###############################################################################

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script as root: sudo -E bash debian-deploy-commands-local-folder.sh" >&2
  exit 1
fi

if [ -z "$SOURCE_DIR" ]; then
  printf "Enter the full path to the prepared repository folder: "
  if ! read -r SOURCE_DIR; then
    echo "Set SOURCE_DIR before running when no interactive prompt is available." >&2
    echo "Example: export SOURCE_DIR=\"/root/uploads/asterisk-pbx-config-portal\"" >&2
    exit 1
  fi
fi

SOURCE_DIR="${SOURCE_DIR%/}"

if [ -z "$SOURCE_DIR" ] || [ ! -d "$SOURCE_DIR" ]; then
  echo "SOURCE_DIR must be an existing directory containing the prepared repository." >&2
  exit 1
fi

if [ ! -f "$SOURCE_DIR/manage.py" ] || [ ! -f "$SOURCE_DIR/requirements.txt" ]; then
  echo "SOURCE_DIR does not look like the portal repository." >&2
  echo "Expected to find manage.py and requirements.txt in: $SOURCE_DIR" >&2
  exit 1
fi

###############################################################################
# 3. Install Debian packages and base services
###############################################################################

apt-get update

# Core tooling:
# - curl/gnupg/ca-certificates for the Docker repository
# - rsync for copying the prepared local source folder into place
# - openssl for generated secrets
# - nginx for reverse proxy
# - ufw/fail2ban for host hardening
# - postgresql-client for manual database administration from the host
apt-get install -y \
  ca-certificates \
  curl \
  fail2ban \
  gnupg \
  nginx \
  openssl \
  postgresql-client \
  rsync \
  ufw

systemctl enable --now nginx
systemctl enable --now fail2ban

###############################################################################
# 4. Install Docker Engine and Docker Compose plugin
###############################################################################

install -m 0755 -d /etc/apt/keyrings

if [ ! -f /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
fi

chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release

cat >/etc/apt/sources.list.d/docker.list <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable
EOF

apt-get update
apt-get install -y \
  containerd.io \
  docker-buildx-plugin \
  docker-ce \
  docker-ce-cli \
  docker-compose-plugin

systemctl enable --now docker

###############################################################################
# 5. Copy the prepared local project source
###############################################################################

install -d -m 0755 "$(dirname "$APP_DIR")"
install -d -m 0755 "$APP_DIR"

SOURCE_DIR_REAL="$(readlink -f "$SOURCE_DIR")"
APP_DIR_REAL="$(readlink -f "$APP_DIR")"

if [ "$SOURCE_DIR_REAL" = "$APP_DIR_REAL" ]; then
  echo "SOURCE_DIR and APP_DIR point to the same directory. Use a separate uploaded source folder." >&2
  exit 1
fi

case "$APP_DIR_REAL/" in
  "$SOURCE_DIR_REAL/"*)
    echo "APP_DIR must not be inside SOURCE_DIR because rsync --delete is used." >&2
    exit 1
    ;;
esac

rsync -a --delete \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude 'media/' \
  --exclude 'staticfiles/' \
  "$SOURCE_DIR_REAL"/ "$APP_DIR"/

cd "$APP_DIR"

install -d -m 0750 "$APP_DIR/media"
install -d -m 0750 "$APP_DIR/staticfiles"
install -d -m 0700 "$BACKUP_DIR"

###############################################################################
# 6. Create production environment file
###############################################################################

# Keep an existing .env file to avoid rotating production secrets accidentally.
if [ ! -f "$APP_DIR/.env" ]; then
  POSTGRES_PASSWORD_GENERATED="$(openssl rand -hex 32)"
  DJANGO_SECRET_KEY_GENERATED="$(openssl rand -hex 48)"

  cat >"$APP_DIR/.env" <<EOF
POSTGRES_DB=portal
POSTGRES_USER=portal
POSTGRES_PASSWORD=${POSTGRES_PASSWORD_GENERATED}
DATABASE_URL=postgres://portal:${POSTGRES_PASSWORD_GENERATED}@db:5432/portal

DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY_GENERATED}
DJANGO_DEBUG=false
DJANGO_ALLOW_SQLITE_FALLBACK=false
DJANGO_ALLOWED_HOSTS=${PORTAL_HOST},localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://${PORTAL_HOST},http://${PORTAL_HOST}

PORTAL_ENFORCE_CLIENT_CIDR=true
PORTAL_ALLOWED_CLIENT_CIDRS=${ALLOWED_CLIENT_CIDRS}
PORTAL_TRUSTED_PROXY_CIDRS=${TRUSTED_PROXY_CIDRS}

PBX_AGENT_PORTAL_URL=https://${PORTAL_HOST}
PBX_ACTIVE_CONFIG_MARKER=/var/lib/asterisk/pbx-active-config.json
PBX_DEPLOYMENT_ALLOWED_ROOTS=/srv/pbx,/opt/pbx,/var/lib/pbx,/etc/asterisk,/srv/tftp,/var/lib/tftpboot,/tftpboot
PBX_RUNTIME_IMAGE_TAG_POLICY=block

GUNICORN_WORKERS=3
GUNICORN_TIMEOUT=120
EOF

  chmod 600 "$APP_DIR/.env"
else
  echo "Keeping existing $APP_DIR/.env"
fi

###############################################################################
# 7. Create production-only Dockerfile
###############################################################################

# The project Dockerfile is intentionally small. This deployment image adds:
# - ffmpeg for IVR prompt conversion
# - postgresql-client so the Django backup workflow can use pg_dump
cat >"$APP_DIR/Dockerfile.deploy" <<'EOF'
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg \
      postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8000
EOF

###############################################################################
# 8. Create production Docker Compose file
###############################################################################

# This avoids the repository's local-development compose command and runs
# Gunicorn behind Nginx. The web port binds only to localhost.
cat >"$APP_DIR/compose.deploy.yml" <<'EOF'
name: asterisk-pbx-config-portal

services:
  web:
    build:
      context: .
      dockerfile: Dockerfile.deploy
    restart: unless-stopped
    env_file:
      - .env
    command: >
      sh -c "python manage.py migrate &&
             python manage.py collectstatic --noinput &&
             gunicorn portal.wsgi:application
               --bind 0.0.0.0:8000
               --workers $${GUNICORN_WORKERS:-3}
               --timeout $${GUNICORN_TIMEOUT:-120}"
    ports:
      - "127.0.0.1:8000:8000"
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - ./media:/app/media
      - ./staticfiles:/app/staticfiles

  db:
    image: postgres:16-alpine
    restart: unless-stopped
    env_file:
      - .env
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-portal}
      POSTGRES_USER: ${POSTGRES_USER:-portal}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U \"$${POSTGRES_USER}\" -d \"$${POSTGRES_DB}\""]
      interval: 5s
      timeout: 5s
      retries: 12
    volumes:
      - postgres_data:/var/lib/postgresql/data

volumes:
  postgres_data:
EOF

###############################################################################
# 9. Create systemd service for the Compose stack
###############################################################################

cat >/etc/systemd/system/pbx-config-portal.service <<EOF
[Unit]
Description=Asterisk PBX Config Portal
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
RemainAfterExit=yes
ExecStart=/usr/bin/docker compose --env-file .env -f compose.deploy.yml up -d --build
ExecStop=/usr/bin/docker compose --env-file .env -f compose.deploy.yml down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now pbx-config-portal.service

###############################################################################
# 10. Configure Nginx reverse proxy
###############################################################################

cat >/etc/nginx/sites-available/pbx-config-portal.conf <<EOF
server {
    listen 80;
    server_name ${PORTAL_HOST};

    client_max_body_size 100m;

    location /static/ {
        alias ${APP_DIR}/staticfiles/;
        access_log off;
        expires 30d;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 120s;
    }
}
EOF

ln -sfn /etc/nginx/sites-available/pbx-config-portal.conf /etc/nginx/sites-enabled/pbx-config-portal.conf

if [ -L /etc/nginx/sites-enabled/default ]; then
  unlink /etc/nginx/sites-enabled/default
fi

nginx -t
systemctl reload nginx

###############################################################################
# 11. Optional TLS certificate through Let's Encrypt
###############################################################################

if [ "$ENABLE_LETS_ENCRYPT" = "true" ]; then
  apt-get install -y certbot python3-certbot-nginx
  certbot --nginx \
    --non-interactive \
    --agree-tos \
    --redirect \
    --email "$LETS_ENCRYPT_EMAIL" \
    -d "$PORTAL_HOST"
fi

###############################################################################
# 12. Optional host firewall
###############################################################################

if [ "$ENABLE_UFW" = "true" ]; then
  ufw allow OpenSSH

  IFS=',' read -r -a CIDR_ARRAY <<<"$ALLOWED_CLIENT_CIDRS"
  for cidr in "${CIDR_ARRAY[@]}"; do
    cidr="$(echo "$cidr" | xargs)"
    if [ -n "$cidr" ]; then
      ufw allow from "$cidr" to any port 80 proto tcp
      ufw allow from "$cidr" to any port 443 proto tcp
    fi
  done

  ufw --force enable
fi

###############################################################################
# 13. Create backup command and timer
###############################################################################

cat >/usr/local/sbin/pbx-config-portal-backup <<EOF
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR}"
BACKUP_DIR="${BACKUP_DIR}"

cd "\$APP_DIR"
set -a
. "\$APP_DIR/.env"
set +a

stamp="\$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "\$BACKUP_DIR"

/usr/bin/docker compose --env-file .env -f compose.deploy.yml exec -T db \
  pg_dump -U "\$POSTGRES_USER" "\$POSTGRES_DB" \
  | gzip >"\$BACKUP_DIR/db-\$stamp.sql.gz"

tar -C "\$APP_DIR" -czf "\$BACKUP_DIR/media-static-\$stamp.tar.gz" media staticfiles

find "\$BACKUP_DIR" -type f -mtime +14 -delete
EOF

chmod 750 /usr/local/sbin/pbx-config-portal-backup

cat >/etc/systemd/system/pbx-config-portal-backup.service <<'EOF'
[Unit]
Description=Back up Asterisk PBX Config Portal
After=pbx-config-portal.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pbx-config-portal-backup
EOF

cat >/etc/systemd/system/pbx-config-portal-backup.timer <<'EOF'
[Unit]
Description=Nightly Asterisk PBX Config Portal backup

[Timer]
OnCalendar=*-*-* 02:15:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now pbx-config-portal-backup.timer

###############################################################################
# 14. Optional interactive admin user creation
###############################################################################

if [ "$CREATE_SUPERUSER_NOW" = "true" ]; then
  docker compose --env-file "$APP_DIR/.env" -f "$APP_DIR/compose.deploy.yml" exec web \
    python manage.py createsuperuser
else
  echo "To create an admin user later, run:"
  echo "  cd $APP_DIR"
  echo "  docker compose --env-file .env -f compose.deploy.yml exec web python manage.py createsuperuser"
fi

###############################################################################
# 15. Health checks and useful operations
###############################################################################

docker compose --env-file "$APP_DIR/.env" -f "$APP_DIR/compose.deploy.yml" ps
curl -fsS http://127.0.0.1:8000/health/ >/dev/null

echo
echo "Deployment finished."
echo "Portal URL: http://${PORTAL_HOST}/"
echo
echo "Useful commands:"
echo "  systemctl status pbx-config-portal.service"
echo "  journalctl -u pbx-config-portal.service -n 100 --no-pager"
echo "  cd $APP_DIR && docker compose --env-file .env -f compose.deploy.yml logs -f web"
echo "  /usr/local/sbin/pbx-config-portal-backup"
echo

###############################################################################
# 16. Optional PBX target host preparation commands
###############################################################################
#
# Run this section on each Debian PBX deployment target, not on the portal host,
# when the portal will SSH deployment bundles to a PBX server.
#
# Install Docker Engine, the Buildx plugin, and the Docker Compose plugin before
# the first exported PBX runtime install. The exported `runtime/asterisk`
# Dockerfile builds patched Asterisk 20.19.0 locally on the PBX host.
#
#   apt-get update
#   apt-get install -y ca-certificates curl gnupg openssh-server rsync tar
#   systemctl enable --now ssh
#   install -d -m 0700 /srv/pbx/releases
#   install -d -m 0700 /srv/pbx/current
#   install -d -m 0700 /srv/pbx/current/asterisk
#   install -d -m 0700 /srv/pbx/current/tftp
#   useradd --system --create-home --shell /bin/bash pbxdeployer || true
#   chown -R pbxdeployer:pbxdeployer /srv/pbx
#
# Add the portal deployment public key to:
#
#   /home/pbxdeployer/.ssh/authorized_keys
#
# Then store the matching private key and known_hosts entry in the portal
# location deployment settings before using Deploy or Rollback.
