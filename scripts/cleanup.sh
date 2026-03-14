#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[cleanup] Removing Python cache directories..."
find . -type d -name '__pycache__' -prune -exec rm -rf {} +

echo "[cleanup] Removing common tool caches..."
rm -rf .pytest_cache .mypy_cache .ruff_cache

echo "[cleanup] Removing Python bytecode files..."
find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

echo "[cleanup] Done."
