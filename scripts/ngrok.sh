#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x ".tools/ngrok" ]; then
  mkdir -p .tools
  curl -L https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz -o .tools/ngrok.tgz
  tar -xzf .tools/ngrok.tgz -C .tools
fi

mkdir -p .ngrok
if [ ! -f ".ngrok/ngrok.yml" ]; then
  cp .ngrok/ngrok.example.yml .ngrok/ngrok.yml
fi

if [ -n "${NGROK_AUTHTOKEN:-}" ]; then
  .tools/ngrok config add-authtoken "$NGROK_AUTHTOKEN" --config .ngrok/ngrok.yml >/dev/null
fi

.tools/ngrok http --config .ngrok/ngrok.yml 5000
