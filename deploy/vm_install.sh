#!/usr/bin/env bash
# Install / update Recal on the Compute Engine VM. Run from the repo checkout:
#
#   bash deploy/vm_install.sh                 # public URL https://<ip-with-dashes>.sslip.io
#   DOMAIN=recal.example.com bash deploy/vm_install.sh   # your own domain (A record -> VM IP)
#
# Re-run it after every `git pull` to redeploy. What it sets up:
#   - backend: uvicorn on 127.0.0.1:8000 as systemd service "recal" (1 worker: OAuth
#     state and graph caches live in-process), serving the built frontend too
#   - Caddy on :80/:443 with an automatic Let's Encrypt certificate (Google OAuth
#     requires an https redirect URI on a real domain)
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(id -un)"
md() { curl -fs -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/$1"; }

IP="$(md instance/network-interfaces/0/access-configs/0/external-ip)"
PROJECT_ID="$(md project/project-id)"
DOMAIN="${DOMAIN:-${IP//./-}.sslip.io}"
BASE_URL="https://$DOMAIN"

echo "==> System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git curl caddy \
  espeak-ng libespeak-ng1 fonts-dejavu-core >/dev/null
# vite 8 needs Node >= 20.19 / 22.12; Ubuntu's nodejs is older
if ! node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=12)?0:1)' 2>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
  sudo apt-get install -y -qq nodejs >/dev/null
fi

echo "==> .env"
cd "$APP_DIR"
if [ ! -f .env ]; then
  cat > .env <<EOF
MOCK=
SESSION_SECRET=$(openssl rand -hex 32)
GOOGLE_CLOUD_PROJECT=$PROJECT_ID
GOOGLE_CLOUD_LOCATION=global
RECAL_MEDIA_BUCKET=${PROJECT_ID}-recal-media
RECAL_ENCRYPTION_KEY=$(openssl rand -base64 32)
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
OAUTH_REDIRECT_BASE=$BASE_URL
LOCAL_TTS=1
EOF
  echo "    created .env — fill GOOGLE_OAUTH_CLIENT_ID / _SECRET, then re-run this script"
fi
set_env() {   # set_env KEY VALUE: replace or append
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
set_env OAUTH_REDIRECT_BASE "$BASE_URL"
set_env GOOGLE_CLOUD_LOCATION global
# the VM's service account is the identity; a copied key file would override it
sed -i '/^GOOGLE_APPLICATION_CREDENTIALS=/d' .env
chmod 600 .env

echo "==> Backend (Python venv)"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> Frontend (vite build -> frontend/dist, served by the backend)"
(cd frontend && npm ci --no-audit --no-fund --loglevel=error && npm run build >/dev/null)

echo "==> systemd service: recal"
sudo tee /etc/systemd/system/recal.service >/dev/null <<EOF
[Unit]
Description=Recal backend (FastAPI + built frontend)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable recal >/dev/null 2>&1
sudo systemctl restart recal

echo "==> Caddy (HTTPS for $DOMAIN)"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$DOMAIN {
    encode gzip
    request_body {
        max_size 50MB
    }
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
    }
}
EOF
sudo systemctl enable caddy >/dev/null 2>&1
sudo systemctl reload caddy || sudo systemctl restart caddy

echo "==> Health check"
for _ in $(seq 1 30); do
  curl -fs http://127.0.0.1:8000/api/health >/dev/null && break
  sleep 1
done
curl -fs http://127.0.0.1:8000/api/health |
  python3 -c 'import json,sys; h=json.load(sys.stdin); print("    mode:", h["mode"]); [print("    !", p) for p in h["problems"]]'

cat <<EOF

Recal is up:  $BASE_URL
  Live model check:   $BASE_URL/api/health?live=1
  Logs:               journalctl -u recal -f
  OAuth redirect URI: $BASE_URL/api/connect/google/callback   (add it to the OAuth client)
EOF
