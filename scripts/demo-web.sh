#!/usr/bin/env bash
# Run the web page against the fake agents in tests/fakes, to try it out
# without calling Claude or Codex. Its discussions go to .demo/ in this folder.
# FAKE_SLEEP sets how many seconds each fake agent takes (default 4).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$root/.demo"
export FUGUE_HOME="$root/.demo/home"
export FUGUE_CONFIG="$root/.demo/no-config.toml"
export FUGUE_CLAUDE_BIN="$root/tests/fakes/claude"
export FUGUE_CODEX_BIN="$root/tests/fakes/codex"
export FAKE_LOG_DIR="$root/.demo"
export FAKE_SLEEP="${FAKE_SLEEP:-4}"
exec uv run --project "$root" fugue web --port "${FUGUE_PORT:-8799}" "$@"
