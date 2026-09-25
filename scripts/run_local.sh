#!/usr/bin/env bash
# Run Buddy Teacher on a laptop (macOS / Linux / WSL) for testing.
# Needs Python 3.10+ and Ollama (https://ollama.com/download) with the model pulled:
#   ollama pull qwen3:4b
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -d .venv ]] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt -r requirements-dev.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  python3 - <<'PY'
import re, secrets
p = ".env"; s = open(p).read()
s = re.sub(r"^SESSION_SECRET=.*$", "SESSION_SECRET=" + secrets.token_hex(32), s, flags=re.M)
open(p, "w").write(s)
PY
  echo "Created .env: add ANTHROPIC_API_KEY and set the two passwords, then re-run."
  exit 0
fi

curl -sf http://127.0.0.1:11434/api/tags >/dev/null || echo "Warning: Ollama isn't running; local answers will fall back to Claude."
exec uvicorn buddy.app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --reload
