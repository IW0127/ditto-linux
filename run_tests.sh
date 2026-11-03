#!/usr/bin/env bash
# Run the full test-suite: unit, CLI, UI (offscreen) and end-to-end (private X server).
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1
python3 -m compileall -q ditto_linux tests >/dev/null
python3 -m unittest -v tests.test_db tests.test_cli tests.test_ui tests.test_maintenance tests.test_e2e "$@"
