#!/usr/bin/env bash
set -Eeuo pipefail
SO101_INTEGRATION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SO101_INTEGRATION_ROOT/run.sh" --integration "$SO101_INTEGRATION_ROOT/integration/recipes.json" "$@"
