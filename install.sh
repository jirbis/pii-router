#!/bin/bash
# PII Router installer: a venv in ~/Library/Application Support/pii-router with the
# packages for your Mac, then the proxy as a LaunchAgent.
#
#   ./install.sh                       profile picked from the hardware
#   ./install.sh --profile minimal     rules + dictionary + optional LLM, no models (~30 MB)
#   ./install.sh --profile intel       + Presidio/spaCy + GLiNER on PyTorch 2.2.2 (~2 GB)
#   ./install.sh --profile apple-silicon
#   ./install.sh --codex --claude      also point the agents at the proxy
#   ./install.sh --no-service          only the venv and the `pii-router` command
set -euo pipefail
cd "$(dirname "$0")"

SUPPORT="${PII_ROUTER_HOME:-$HOME/Library/Application Support/pii-router}"
VENV="$SUPPORT/venv"
OLD_VENV="$HOME/Library/Application Support/codex-pii-router/venv"
PROFILE=""
SERVICE=1
AGENTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift ;;
    --no-service) SERVICE=0 ;;
    --codex) AGENTS+=(--codex) ;;
    --claude) AGENTS+=(--claude) ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
if [ -z "$PROFILE" ]; then
  [ "$(uname -m)" = "arm64" ] && PROFILE=apple-silicon || PROFILE=intel
fi
[ -f "requirements/$PROFILE.txt" ] || { echo "no profile $PROFILE (minimal | intel | apple-silicon)" >&2; exit 2; }

pick_python() {
  for p in python3.12 python3.11 python3.10 /usr/local/bin/python3 /opt/homebrew/bin/python3 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(not (3,10) <= sys.version_info[:2] <= (3,12))' 2>/dev/null; then
      command -v "$p"; return
    fi
  done
  echo "need Python 3.10-3.12 (brew install python@3.12)" >&2; exit 1
}

mkdir -p "$SUPPORT"
if [ ! -x "$VENV/bin/python" ] && [ -x "$OLD_VENV/bin/python" ]; then
  echo "reusing the venv of codex-pii-router (moved to $VENV)"
  mv "$OLD_VENV" "$VENV"
fi
if [ ! -x "$VENV/bin/python" ]; then
  PY="$(pick_python)"
  echo "creating $VENV with $PY"
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q --upgrade pip
echo "installing profile $PROFILE"
"$VENV/bin/python" -m pip install -q -r "requirements/$PROFILE.txt"
"$VENV/bin/python" -m pip install -q --no-deps --force-reinstall .
if [ "$PROFILE" != minimal ]; then
  for m in en_core_web_md de_core_news_md ru_core_news_md; do
    "$VENV/bin/python" -c "import $m" 2>/dev/null || "$VENV/bin/python" -m spacy download -q "$m" || true
  done
fi

mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/pii-router" "$HOME/.local/bin/pii-router"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to PATH for the pii-router command" ;; esac
command -v zstd >/dev/null 2>&1 || echo "note: the Codex app compresses requests with zstd: brew install zstd"

if [ "$SERVICE" = 1 ]; then
  "$VENV/bin/pii-router" install ${AGENTS[@]+"${AGENTS[@]}"}
fi
"$VENV/bin/pii-router" doctor | sed -n '/"advice"/,$p'
