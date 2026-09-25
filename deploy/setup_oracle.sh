#!/usr/bin/env bash
# Buddy Teacher on Oracle Cloud Always Free (Ubuntu 24.04, Ampere A1.Flex 4 OCPU / 24 GB, no GPU).
#
# Run from a checkout of this repo on the server:
#   sudo TS_AUTHKEY=tskey-... bash deploy/setup_oracle.sh      (TS_AUTHKEY optional)
#
# What it does:
#   - installs Python deps, Ollama (+ the local model) and Tailscale
#   - copies the app to /opt/buddy-teacher, runs it as user "buddy" under systemd
#   - binds the app to 127.0.0.1 and exposes it ONLY on your tailnet via `tailscale serve`
#   - opens no public ports (keep the OCI security list at SSH-only, or close SSH too
#     once `tailscale ssh` works for you)
# Safe to re-run: it updates code and dependencies and keeps data/ and .env.
set -euo pipefail

APP_DIR=/opt/buddy-teacher
APP_USER=buddy
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3:4b}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
[[ "$(uname -m)" == "aarch64" ]] || echo "Note: expected aarch64 (A1.Flex); continuing on $(uname -m)."

echo "==> System packages"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-dev \
  build-essential git curl rsync ca-certificates

echo "==> App user and files"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/buddy \
  --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"
rsync -a --delete --exclude .venv --exclude data --exclude .env --exclude .cache \
  --exclude .git "$SRC_DIR/" "$APP_DIR/"
mkdir -p "$APP_DIR/data" "$APP_DIR/.cache/huggingface"

echo "==> Python environment (CPU-only PyTorch for bge-m3)"
[[ -d "$APP_DIR/.venv" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q torch --index-url https://download.pytorch.org/whl/cpu
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> .env"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  KID_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(6))')"
  PARENT_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
  SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  sed -i "s|^KID_PASSWORD=.*|KID_PASSWORD=$KID_PW|; s|^PARENT_PASSWORD=.*|PARENT_PASSWORD=$PARENT_PW|; \
s|^SESSION_SECRET=.*|SESSION_SECRET=$SECRET|; s|^OLLAMA_MODEL=.*|OLLAMA_MODEL=$OLLAMA_MODEL|" "$APP_DIR/.env"
  echo "    Generated passwords -> kid: $KID_PW   parent: $PARENT_PW  (change them in $APP_DIR/.env)"
  NEED_KEY=1
fi
grep -q '^ANTHROPIC_API_KEY=.\+' "$APP_DIR/.env" || NEED_KEY=1
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Ollama + $OLLAMA_MODEL"
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<CONF
[Service]
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_NUM_PARALLEL=1
Environment=OLLAMA_MAX_LOADED_MODELS=1
CONF
systemctl daemon-reload
systemctl enable --now ollama
for i in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
ollama pull "$OLLAMA_MODEL"

echo "==> Pre-download bge-m3 embeddings (~2.3 GB)"
sudo -u "$APP_USER" HF_HOME="$APP_DIR/.cache/huggingface" "$APP_DIR/.venv/bin/python" -c \
  "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"

echo "==> systemd service"
cp "$APP_DIR/deploy/buddy-teacher.service" /etc/systemd/system/buddy-teacher.service
systemctl daemon-reload
systemctl enable buddy-teacher
systemctl restart buddy-teacher

echo "==> Tailscale (private access only)"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled
if ! tailscale status >/dev/null 2>&1; then
  if [[ -n "${TS_AUTHKEY:-}" ]]; then
    tailscale up --authkey "$TS_AUTHKEY" --hostname buddy-teacher
  else
    tailscale up --hostname buddy-teacher   # prints a login URL; open it to add this server
  fi
fi
# HTTPS on the tailnet only (not Funnel, so nothing is public).
tailscale serve --bg 8000

echo
echo "Done."
echo "  Open on the tablet (with the Tailscale app signed in):"
tailscale serve status 2>/dev/null | grep -Eo 'https://[^ ]+' | head -1 | sed 's/^/    /' || true
echo "  Logs:   journalctl -u buddy-teacher -f"
echo "  Health: curl -s http://127.0.0.1:8000/api/health"
if [[ -n "${NEED_KEY:-}" ]]; then
  echo
  echo "  NEXT: put your Claude key in $APP_DIR/.env (ANTHROPIC_API_KEY=...), then"
  echo "        sudo systemctl restart buddy-teacher"
fi
echo "  Ingest a chapter (as the app user):"
echo "    cd $APP_DIR && sudo -u $APP_USER HF_HOME=$APP_DIR/.cache/huggingface .venv/bin/python -m buddy.ingest run evs 1"
