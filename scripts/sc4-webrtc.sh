#!/usr/bin/env bash
# SC4 over WebRTC (issue #59, ADR 0024-hvoice-webrtc-sc4): the voice contract's two in-process
# WebRTC cases, livekit and pipecat-smallwebrtc, with every timing check enabled.
#
#   scripts/sc4-webrtc.sh [log-dir]      (default log dir: .ci-local/ in the repo)
#
# The merge gate skips these cases' timing checks (HECKLR_SHARED_CPU=1, ADR 0010b) because
# their reference bots share the test process with the harness and their own record of when
# they spoke is the timing truth. This script is the dedicated-CPU opt-in: HECKLR_SHARED_CPU
# unset, one process (no xdist), on a machine with idle cores. It prints the load before and
# after, as the result depends on it, and fails if a check was skipped: the livekit case needs
# Docker for its LiveKit server, and a skipped check would prove nothing.
set -u
root=$(cd "$(dirname "$0")/.." && pwd) || exit 2
dir=${1:-$root/.ci-local}
out=$(mkdir -p "$dir" && cd "$dir" && pwd) || exit 2
cd "$root" || exit 2
unset HECKLR_SHARED_CPU OPENAI_API_KEY ANTHROPIC_API_KEY ASSEMBLYAI_API_KEY
log="$out/sc4-webrtc.log"

cores=$(getconf _NPROCESSORS_ONLN)
echo "cores $cores, load before: $(uptime | sed 's/.*load averages*: //')"
uv run pytest packages/hecklr-adapters/tests/contract/test_voice_contract.py -p no:xdist \
  -k "livekit or pipecat-smallwebrtc" -rs >"$log" 2>&1
status=$?
echo "load after: $(uptime | sed 's/.*load averages*: //')"
tail -1 "$log"
# WebRTC reference bots cannot end a call abnormally, so that one contract test never applies to
# them; any other skip (no Docker, a shared CPU) means a check did not run.
if grep "^SKIPPED" "$log" | grep -v "cannot fail abnormally on this transport"; then
  echo "FAIL: a WebRTC check was skipped (see $log)"
  exit 1
fi
[ "$status" = 0 ] || echo "FAIL: see $log"
exit "$status"
