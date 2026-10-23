# Roadmap

Hecklr is built in waves from its
[specification](https://github.com/ebenbaruk/hecklr/blob/main/docs/SPEC.md); each wave closes on a
milestone report with evidence for every criterion.

## Done

- **Foundation:** frozen schemas, the provider layer with a deterministic fake model and a response
  cache, CI.
- **Chat (M1):** orchestrator, simulator (scripted, guided, replay, adversarial), world sandbox
  over MCP and HTTP, graders and judges, statistics, reports, the CLI and the GitHub Action, and the
  reference agent with faults F1-F6, each caught.
- **Voice and email (M2):** the voice engine (impairments, turn-taking, metrics), WebSocket, Twilio,
  Pipecat and LiveKit transports, the loopback calibration (SC4), email over SMTP/IMAP and webhooks,
  and faults F7-F12, each caught.
- **Production loop (M3):** the miner, the adversarial pack with safety graders, and the local UI.
- **Fixer** (`hecklr fix`): candidate prompt patches validated against the suite (targeted and
  guard passes), with `--pr`.
- **Benchmark** (`hecklr bench`): a pinned suite, eight stacks, `reproduce`, and a static
  leaderboard.
- **Hardening:** robustness of run, gate and report; simulator guard fixes (F4 caught on the full
  `matrix-live` suite).

## In progress (toward v1.0)

- **Benchmark numbers:** the measured rows are preliminary (a shared, loaded laptop; local qwen3
  customers and judges). Re-measure on an idle machine before
  publishing the leaderboard.
- **Docs and launch:** this site, the demo, the launch posts.
- `agent.command` in `hecklr.yaml` instead of `hecklr fix --agent-cmd` (RFC 0015a, v1.1).

## Open items on the way to v1.0

Tracked as issues; the milestone reports list them with evidence.

- SC3 (50 simultaneous calls on 8 cores) as a CI benchmark, not only a laptop measurement.
- SC4 asserted over WebRTC transports on a dedicated idle runner.
- The voice fault matrix (F7-F10) and F10 as a nightly job on `main`.
- The adaptive design's trial savings measured and published.
- First PyPI release and a Docker image.

## Later

- `webrtc` (WHIP-style) and `sip` voice adapters, and the chat `webhook` adapter.
- S3 and GCS baseline stores.

Suggestions are welcome as GitHub issues.
