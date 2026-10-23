# 00hra. Runs that measured nothing, oversized replies and interrupted runs

Date: 2026-10-23 · Status: proposed · Workstream: WShrun (red-team issues #33, #34, #35)

## Context

The M1 red team found three ways a run can look like a measurement when it is not one:

- **#33.** When every trial ends in an agent error or a timeout (agent down, wrong URL, HTTP 500
  on every message, a reply the adapter cannot parse, an agent that hangs), the run is
  `completed`, `hecklr run` exits 0 and `hecklr gate --missing-baseline pass` (the Action's
  default) passes it. On `main` the Action then stores it as the baseline, against which nothing
  can ever regress. Agent errors and timeouts are failed trials, which is right for statistics
  (ADR 0007b), but a run in which not one conversation got through measured the setup.
- **#34.** Nothing capped what an agent may send back: a stub agent answering 20 MB per message
  turned a 33-trial run into a 2.9 GB store and a 3.0 GB "self-contained" HTML report.
- **#35.** Ctrl-C recorded the run as `cancelled` correctly but printed a raw `CancelledError`
  traceback logged by the world sandbox's uvicorn lifespan task.

The spec fixes the gate's exit codes (0 pass, 1 regression, 2 inconclusive, 3 harness error) and
`hecklr run` already reuses 0-3 (`hecklr.exit_codes`).

## Decision

- **No trial got through = harness error, exit 3.** "Got through" means at least one trial ended
  `ok`. When none did and at least one ended `agent_error` or `timeout`,
  `hecklr_report.analysis.nothing_got_through` names the tally and the first error.
  `hecklr run` prints it and exits **3**; `analyze` adds it to `Analysis.problems` for the
  candidate and for the baseline, so `hecklr gate` exits 3 whatever `--missing-baseline` says
  and the Action never stores such a run as a baseline. Exit 3 rather than a new code: the
  gate's contract is 0-3 and "the measurement cannot be trusted" is exactly what 3 means;
  1 would claim a regression the run cannot show. Exit 1 stays for real regressions: a single
  `ok` trial makes the run a measurement again, and a candidate that breaks most conversations
  regresses against its baseline as before. The run's status stays `completed` (the harness did
  finish; `RunStatus` is frozen). The `baseline` alias skips such runs (their recorded counts
  show finished trials and none `ok`), so once the agent is fixed, a local `hecklr gate`
  compares with the last real measurement instead of refusing to gate.
- **Agent reply cap.** Every chat target is wrapped by `hecklr_adapters.cap_reply_size`: one
  agent turn may carry at most `max_reply_bytes` of UTF-8 text (`hecklr run --max-reply-bytes`,
  `RunRequest.max_reply_bytes`, default 256 KiB, far above any model's output limit). Past it the
  head up to the cap is kept in the transcript and the trial is an `agent_error` ("agent reply
  over N bytes"), so a flooding agent fails its trials instead of filling the store. The cap is a
  run setting, not a `hecklr.yaml` field, because `HecklrConfig` is frozen.
- **Report size.** The HTML trial viewer shows at most 10,000 characters of any event text or
  JSON detail, with a "truncated, N more characters" marker; the stored run keeps everything.
- **Ctrl-C.** `hecklr run` prints one line ("Interrupted: run <id> recorded as cancelled") and
  exits 130 (128 + SIGINT, the shell convention). Records logged by `uvicorn.error` whose
  exception is a cancellation are dropped while the run executes: a cancelled server is not an
  error.

## Consequences

- A broken agent setup fails CI loudly on the first run, including a repository's first PR.
- A legitimate agent that sends replies over 256 KiB must raise `--max-reply-bytes`.
- Email bodies and voice audio are not capped here: audio is bounded by the call's duration;
  email bodies are rendered truncated in the report but stored whole.
