# Roadmap

Hecklr was built in waves from its
[specification](https://github.com/ebenbaruk/hecklr/blob/main/docs/SPEC.md); each wave closed on a
milestone report with evidence for every criterion. **v1.0 is done**: all ten success criteria
are met, three of them (SC3, SC7, SC10) as the owner scoped them for v1.0
([v1.0 report](https://github.com/ebenbaruk/hecklr/blob/main/docs/milestones/V1.0.md),
[ADR 0024](https://github.com/ebenbaruk/hecklr/blob/main/docs/adr/0024-hdocs-v1-scope-decisions.md)).

## Done in v1.0

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
  leaderboard. The measured rows are preliminary.
- **Hardening:** two red-team passes; every P0/P1 finding fixed, P2/P3 findings tracked in the
  [v1.0 report](https://github.com/ebenbaruk/hecklr/blob/main/docs/milestones/V1.0.md)'s issue closure.

## At launch

These need a public repository, which is the owner's decision
([launch checklist](https://github.com/ebenbaruk/hecklr/blob/main/docs/milestones/LAUNCH.md)):

- The first PyPI release, so that `pip install hecklr` works, and a Docker image.
- This documentation site, hosted.
- The benchmark re-measured on an idle machine, then the leaderboard published.
- At least 10 `good first issue` issues for new contributors.

## Optional follow-ups

- A human review of the judge calibration labels (today AI-drafted), then the live κ run with
  `--labeler human:` ([judge calibration](guides/judge-calibration.md)).
- SC3 (50 simultaneous calls) measured on an 8-core runner as a scheduled job.
- SC4 over WebRTC (LiveKit, SmallWebRTC) in a scheduled job on a dedicated runner; today it is a
  release check, `scripts/sc4-webrtc.sh` (ADR 0024-hvoice).
- The voice fault matrix (F7-F10) as a nightly job on `main`.
- The adaptive design's trial savings measured and published.
- `agent.command` in `hecklr.yaml` instead of `hecklr fix --agent-cmd` (RFC 0015a).

## Later

- `webrtc` (WHIP-style) and `sip` voice adapters, and the chat `webhook` adapter.
- S3 and GCS baseline stores.

Suggestions are welcome as GitHub issues.
