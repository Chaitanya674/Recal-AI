#!/usr/bin/env bash
# Install / update Recal on the Compute Engine VM. Run from the repo checkout:
#
#   bash deploy/vm_install.sh                 # public URL https://<ip-with-dashes>.sslip.io
#   DOMAIN=recal.example.com bash deploy/vm_install.sh   # your own domain (A record -> VM IP)
#
# Re-run it after every `git pull` to redeploy. What it sets up:
#   - backend: uvicorn on 127.0.0.1:8000 started with nohup by deploy/start.sh (1 worker:
#     OAuth state and graph caches live in-process), serving the built frontend too;
#     restarted on reboot by an @reboot cron entry
#   - Caddy on :80/:443 with an automatic Let's Encrypt certificate (Google OAuth
#     requires an https redirect URI on a real domain)
#   - no logs on disk: app output -> /dev/null, system journal kept in RAM only
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
md() { curl -fs -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/$1"; }

IP="$(md instance/network-interfaces/0/access-configs/0/external-ip)"
PROJECT_ID="$(md project/project-id)"
DOMAIN="${DOMAIN:-${IP//./-}.sslip.io}"
BASE_URL="https://$DOMAIN"

# small VMs (e2-small, 2 GB): 2 GB of swap keeps the frontend build and reel rendering
# from being OOM-killed
if [ "$(free -m | awk '/^Mem:/{print $2}')" -lt 3500 ] && ! swapon --show | grep -q /swapfile; then
  echo "==> Adding 2 GB swap"
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git curl caddy cron \
  espeak-ng libespeak-ng1 fonts-dejavu-core >/dev/null
# vite 8 needs Node >= 20.19 / 22.12; Ubuntu's nodejs is older
if ! node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=12)?0:1)' 2>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
  sudo apt-get install -y -qq nodejs >/dev/null
fi
sudo apt-get clean

echo "==> Logs: system journal in RAM only (16 MB cap), nothing written to disk"
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]
Storage=volatile
RuntimeMaxUse=16M
' |
  sudo tee /etc/systemd/journald.conf.d/recal.conf >/dev/null
sudo systemctl restart systemd-journald
sudo rm -rf /var/log/journal/* "$APP_DIR/logs"

echo "==> .env"
cd "$APP_DIR"
ENV_SECRET="${ENV_SECRET:-recal-env}"
if gcloud secrets describe "$ENV_SECRET" >/dev/null 2>&1; then
  # source of truth: Secret Manager (needs roles/secretmanager.secretAccessor on the VM's SA)
  (umask 077; gcloud secrets versions access latest --secret="$ENV_SECRET" > .env)
  echo "    pulled from Secret Manager: $ENV_SECRET (latest version)"
elif [ ! -f .env ]; then
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
.venv/bin/pip install -q --no-cache-dir --upgrade pip
.venv/bin/pip install -q --no-cache-dir -r requirements.txt

echo "==> Frontend (vite build -> frontend/dist, served by the backend)"
# node_modules and the npm cache are only needed for the build: removed to save disk
(cd frontend && npm ci --no-audit --no-fund --loglevel=error && npm run build >/dev/null)
rm -rf frontend/node_modules
npm cache clean --force >/dev/null 2>&1 || true

echo "==> Backend: nohup (deploy/start.sh), restarted on reboot via cron"
if [ -f /etc/systemd/system/recal.service ]; then   # older systemd setup
  sudo systemctl disable --now recal >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/recal.service && sudo systemctl daemon-reload
fi
bash deploy/start.sh
(crontab -l 2>/dev/null | grep -v "# recal-start" || true
 echo "@reboot bash $APP_DIR/deploy/start.sh >/dev/null 2>&1 # recal-start") | crontab -

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
for _ in $(seq 1 90); do      # first boot on a 2 GB VM can take a while
  curl -fs http://127.0.0.1:8000/api/health >/dev/null && break
  sleep 1
done
curl -fs http://127.0.0.1:8000/api/health |
  python3 -c 'import json,sys; h=json.load(sys.stdin); print("    mode:", h["mode"]); [print("    !", p) for p in h["problems"]]' ||
  echo "    backend not answering yet — see why with: bash deploy/start.sh fg"

cat <<EOF

Recal is up:  $BASE_URL
  Live model check:   $BASE_URL/api/health?live=1
  Restart backend:    bash deploy/start.sh        (stop: bash deploy/start.sh stop)
  Debug (live logs):  bash deploy/start.sh fg     (on screen only; Ctrl+C, then start.sh)
  Disk usage:         df -h /
  OAuth redirect URI: $BASE_URL/api/connect/google/callback   (add it to the OAuth client)
EOF
