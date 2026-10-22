#!/usr/bin/env bash
# AriaStreamLab bootstrap (macOS / Linux): a Python environment the SDK wheels support,
# AriaStreamLab installed into it, and the application started.
#
#   bash scripts/install.sh            # from a clone of the repository
#   bash scripts/install.sh --sdk      # also install Meta's SDK (needs Python 3.10–3.12)
#   bash scripts/install.sh --no-app   # do not start the application at the end
#
# Everything the script does can also be done from the application's Setup page once it runs
# — except creating this environment, which is why the script exists.
set -euo pipefail

WITH_SDK=0; START_APP=1
for arg in "$@"; do
  case "$arg" in
    --sdk) WITH_SDK=1 ;;
    --no-app) START_APP=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
PY=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    ver="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    case "$ver" in
      3.10|3.11|3.12) PY="$candidate"; break ;;
    esac
  fi
done
if [ -z "$PY" ]; then
  echo "No Python 3.10–3.12 found (these are the versions Meta's SDK supports)." >&2
  echo "Install one — macOS: 'brew install python@3.12'; Ubuntu: 'sudo apt install python3.12-venv'" >&2
  echo "— or use conda: 'conda create -n aria python=3.12 && conda activate aria && pip install -e .'" >&2
  exit 1
fi
echo "using $PY ($("$PY" --version))"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
if [ "$WITH_SDK" = 1 ]; then
  python -m pip install -e ".[aria]"
else
  python -m pip install -e .
fi
echo
ariastreamlab doctor || true
echo
echo "Environment ready: 'source .venv/bin/activate' and then 'ariastreamlab' opens the application."
if [ "$START_APP" = 1 ]; then
  ariastreamlab
fi
