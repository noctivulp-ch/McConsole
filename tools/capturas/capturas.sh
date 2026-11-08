#!/usr/bin/env bash
# Regenera docs/capturas/. Necesita uv y Chromium. Ver capturas.py.
set -euo pipefail
cd "$(dirname "$0")/../.."
uv run --quiet --with pillow --with numpy python tools/capturas/capturas.py
