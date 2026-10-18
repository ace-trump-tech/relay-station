#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
# pip is idempotent and also repairs a previously interrupted installation.
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python run.py
