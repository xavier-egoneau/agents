#!/bin/sh
set -eu

uv run ruff check src tests tools/modules
uv run pytest
npm --prefix surfaces/web run lint
npm --prefix surfaces/web run test:unit
npm --prefix surfaces/web run test:e2e
