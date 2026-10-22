# acme-email results: F11 and F12 caught, clean A/A quiet

Measured 2026-10-22 on macOS arm64 (10 cores), commit of the WS12b PR. Every number below comes
from a command in this file; pass rates are Wilson 95% intervals, deltas paired cluster-bootstrap
intervals (`hecklr_stats.compare_runs`, alpha 0.05, minimum effect 0.03).

## 1. Driver-level matrix over real processes (rules brain, free)

`uv run python examples/acme-email/scripts/driver_matrix.py`: for each variant, the agent runs as
its own process (`serve.py --brain rules [--fault Fn] webhook`); each of the 14 scripted
scenarios is one graded trial through Hecklr's `webhook` adapter and `EmailDriver`, the agent's
tools on a real world `Sandbox` over HTTP (`ACME_WORLD_BASE_URL` + `X-Hecklr-Trial-Id`, since the
adapter on `main` does not forward the world URL yet; every email was served from the sandbox,
none from a local copy).

| Variant | Pass rate | Failure causes | vs clean: verdict, delta [95% CI], p |
| --- | --- | --- | --- |
| clean | 14/14 = 1.00 [0.78, 1.00] | none | (baseline) |
| clean again (A/A) | 14/14 = 1.00 [0.78, 1.00] | none | no_change, +0.00 [+0.00, +0.00], p = 1.0000 |
| F11 | 0/14 = 0.00 [0.00, 0.22] | `email_threading` x14 | **regression**, -1.00 [-1.00, -1.00], p = 0.0001 |
| F12 | 8/14 = 0.57 [0.33, 0.79] | `timeout` x6 (+ `state` x3, `regex` x3 on the same trials) | **regression**, -0.43 [-0.71, -0.21], p = 0.0312 |

- F11 fails exactly `email_threading`, on every scenario, and nothing else.
- F12 fails exactly the 6 `catches-f12` scenarios (a hung `get_policy` or
  `create_emergency_ticket`), each as a `timeout`: the customer's email was never answered (email
  dead air). Its agent's `/healthz` counted 6 `replies_failed`: the late answers, sent after the
  sandbox released the hung calls at trial end, to conversations that no longer existed.
- The clean agent answered all 35 emails of each run in thread.

## 2. In-process matrix and transports (`pytest`, rules brain, free)

`uv run pytest examples/acme-email -n auto --cov=examples/acme-email/agent`: 102 passed, 94%
line+branch coverage of `agent/`.

- `test_suite.py`: `test_sc8_clean_email_agent_passes_every_scripted_scenario` (14/14),
  `test_sc8_f11_fails_threading_on_every_scripted_scenario` (14/14, only `email_threading`),
  `test_sc8_f12_fails_exactly_the_tool_timeout_scenarios` (6 `timeout`, 8 pass); every suite of
  both configs resolves with `hecklr_core`.
- `test_webhook.py`: the agent's webhook server driven by the `webhook` adapter on a real sandbox:
  clean, F11 and F12 cases; the `X-Hecklr-World-Url` header (what WS-wire's adapter sends) beats
  `ACME_WORLD_BASE_URL`; an undeliverable reply is counted and logged.
- `test_mailbox.py`: GreenMail in Docker, Hecklr's `smtp-imap` adapter, the agent polling IMAP and
  answering over SMTP: clean passes, F11 fails `email_threading`, F12 times out; mail is answered
  once across polls.

## 3. LLM brain on luna (`pytest -m live`)

`uv run --env-file .env pytest examples/acme-email/tests/test_live_email.py -m live`: 3 passed in
116 s, each under its $0.02 cap.

- `test_llm_email_agent_moves_a_visit_in_thread`: every criterion passes (state, threading).
- `test_llm_email_agent_answers_although_the_ticket_tool_hangs`: the clean agent answers in thread
  after the ticket tool times out.
- `test_sc8_f12_llm_email_agent_goes_silent_when_the_ticket_tool_hangs`: under F12 no reply, trial
  `timeout`.

## 4. `hecklr run --channels email` + `hecklr compare` (smoke, rules brain, $0)

`examples/acme-email/scripts/email-fault-matrix.sh`, run on a local, unpushed merge of this
branch with WS-wire's `ws-wire-voice-email-compose` (f4f631e), which adds email to `hecklr run`
and forwards the world URL. Four real `hecklr run -s smoke --channels email` runs against the
agent process, then `hecklr compare` of each with the first clean run:

| Comparison | Verdict | Pass rate | Δ [95% CI], p | Flipped pass → fail, by cause |
| --- | --- | --- | --- | --- |
| clean vs clean (A/A) | NO CHANGE | 100.0% → 100.0% | 0.0 pts [0.0, 0.0], p = 1.000 | none |
| clean vs F11 | **REGRESSION** | 100.0% → 0.0% | −100.0 pts [−100.0, −100.0], p < 0.001 | `email_threading` (14): "no In-Reply-To" |
| clean vs F12 | **REGRESSION** | 100.0% → 57.1% [28.6%, 78.6%] | −42.9 pts [−71.4, −21.4], p = 0.031 | the 6 `catches-f12` scenarios, 6 `timeout` trials (+ `regex` 3, `state` 3) |

The agent's `/healthz` after each run: every email's tools went to the trial's sandbox through
the webhook body's `world_url` (35 of 35 emails; F12: 32, none local); F12 counted 6
`replies_failed`, the late answers to conversations that had already timed out.

To repeat after WS-wire merges (from the repository root):

```bash
examples/acme-email/scripts/email-fault-matrix.sh                                   # smoke, $0
SUITE=ci BRAIN=llm BUDGET_USD=0.25 examples/acme-email/scripts/email-fault-matrix.sh  # luna
```
