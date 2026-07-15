#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x ".venv/bin/flask" ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi

.venv/bin/flask --app app run --host 0.0.0.0 --port 5000
