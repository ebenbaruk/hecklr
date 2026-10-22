# acme-email results

Summary: on the free scripted `smoke` suite, F11 and F12 are caught and clean vs clean is quiet
(sections 1, 2 and 4). On the guided luna `ci` suite, the first matrix failed the clean agent
(38.9%) and left both faults inconclusive. After the fix, the clean agent passes 36/36, but the
OpenAI account ran out of credit during the next run, so F11 and F12 have not been measured on
`ci` yet (section 5).

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

Repeated after the review round (120 s reply deadline; the script now takes each run id from
the CLI's output), on a merge with WS-wire `ad883be`: clean vs clean NO CHANGE (100.0% [78.5%,
100.0%] both), clean vs F11 REGRESSION (0.0% [0.0%, 21.5%]), clean vs F12 REGRESSION (57.1%
[32.6%, 78.6%]); runs `01M5GQ2MH8YSPHENT6VYARFAAA`, `01M5GQ3BP70DKH3R4NNHYF8D64`,
`01M5GQ43NBSZXCV24MH63QFN3P`, `01M5GQ4TM6P5MVJ56EA6SBE9TM`.

To repeat after WS-wire merges (from the repository root):

```bash
examples/acme-email/scripts/email-fault-matrix.sh                                   # smoke, $0
```

## 5. The guided `ci` suite on luna (`hecklr run` + `hecklr compare`, LLM brain)

`SUITE=ci BRAIN=llm BUDGET_USD=0.15 examples/acme-email/scripts/email-fault-matrix.sh` (with
`OPENAI_API_KEY` exported), on a local merge of this branch with WS-wire. The suite runs 13
scenarios x 3 trials. The refund scenario's 3 trials are always harness errors: the simulator's
consistency guard rejects the customer's first email (issue #29), so they are excluded from the
statistics.

### 5a. First matrix, before the fix: the clean agent failed `ci`

The suite then had a 45 s reply deadline and the `resolved` judge on every scenario.

| Run | Pass rate (scenario-level, n 12) | Timeouts | vs clean: verdict, Δ [95% CI], p |
| --- | --- | --- | --- |
| clean | 38.9% [13.7%, 66.5%] | 9 | (baseline); `judge:resolved` failed all 7 emergency and policy-hang scenarios (0/3 each) |
| clean again | 41.7% [19.3%, 68.0%] | 9 | INCONCLUSIVE, +2.8 pts [0.0, +11.1], p = 1.000 |
| F11 | 0.0% [0.0%, 24.2%] | 11 | INCONCLUSIVE, −38.9 pts [−66.7, −13.9], p = 0.062 |
| F12 | 41.7% [19.3%, 68.0%] | 18 | INCONCLUSIVE, +2.8 pts [0.0, +11.1], p = 1.000 |

F11 could flip only the 5 scenarios that the clean agent passed, which is too few for
significance. F12 could not flip anything, because the clean agent already failed every scenario
with a hung tool. The suite could not serve as a gate. Three causes:

1. The 45 s reply deadline was shorter than a clean LLM agent retrying a hung tool under its 5 s
   tool deadline, so clean trials timed out too.
2. The `resolved` judge failed the clean agent on every email emergency, `email-gas-smell`
   included (no hung tool): by email the agent cannot hand the customer to the on-call engineer.
3. It also failed the agent on the fee question of the policy-hang cancellations: the fee cannot
   be quoted while `get_policy` hangs.

### 5b. After the fix

The fix: a 120 s reply deadline; no `resolved` on the emergencies, which `tool_called` and the
reply deadline grade; on the policy-hang cancellations, `resolved` stays, and the goal accepts
"the fee cannot be checked right now".

| Run | Pass rate | Notes |
| --- | --- | --- |
| clean (`01M5GKP2T5X81J6ND8QEB2CHHJ`) | **36/36 trials; scenario-level 100.0% [75.8%, 100.0%]**, 0 timeouts, $0.058 | every scenario 3/3 |
| clean again (`01M5GMEGS5R3TSW1ED280XF723`) | 86.1% [66.5%, 99.9%], 5 timeouts | **not valid**: the account ran out of credit during this run (below) |
| F11 (`01M5GNANTD9DR48JMGC8PZ44X6`) | 0/36, 36 timeouts | **void**: every brain call got `429 credit_balance_exhausted`, and the agent sent no reply |
| F12 | not run | stopped once the account had no credit |

The second clean run is not usable as A/A evidence. From 03:55 the agent's log has
`429 insufficient_quota / credit_balance_exhausted` errors on its replies. At least 3 of the 5
timed-out trials match those failed replies by customer address. The F11 run was void for the
same reason. So **F11 and F12 have not been measured on the guided `ci` suite.**

What the valid clean run does show for F12: the hung tool was hit in all 18 trials of the 6
`catches-f12` scenarios (`get_policy` or `create_emergency_ticket` returned the injected timeout
9 to 99 times per scenario), and the clean agent answered anyway. Under F12
(`tool_timeout_s: null`) the first such call blocks the reply for good. That is the behaviour
measured on luna by `test_sc8_f12_llm_email_agent_goes_silent_when_the_ticket_tool_hangs`
(section 3) and on `smoke` (sections 1, 2 and 4). F11 removes the threading headers whatever the
brain says. Both faults are expected to show on `ci`, but neither has been shown there yet.

To finish the measurement once the OpenAI account has credit again (about $0.25 for the four
runs; the agent's own luna calls come on top):

```bash
set -a; source .env; set +a
SUITE=ci BRAIN=llm BUDGET_USD=0.15 examples/acme-email/scripts/email-fault-matrix.sh
```
