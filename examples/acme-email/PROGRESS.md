# acme-email progress

Owner: WS12b (email reference agent, faults F11 and F12). Spec: `docs/SPEC.md` → "Channel
drivers" (email), "Agent adapters" (`EmailTarget`, `smtp-imap`, `webhook`), "Graders" (email
threading, `resolved`), "Testing strategy" (fault matrix F11, F12), WS12 row. ADRs: 0006, 0006a,
0007d, 0008a, 0012a, and this workstream's 0012b.

## Plan

1. `agent/acme_email`: the acme agent's brains (imported from `examples/acme-maintenance`) behind
   an email front end: one conversation per customer address, quoted history stripped, replies
   threaded (`In-Reply-To`, `References`, `Re:` subject), tools at the trial's world (body
   `world_url`, `X-Hecklr-World-Url` HTTP or MIME header, `ACME_WORLD_BASE_URL` + trial id, else
   a local copy), every tool call under the front end's deadline.
2. Transports: webhook (FastAPI, `202` then the reply POSTed to `reply_url`), SMTP/IMAP (IMAP
   `UNSEEN` polling, SMTP replies; blocking calls in worker threads), and in process
   (`PythonEmailTarget`) for tests.
3. Faults as one-line overlays of the front end's `mailer.yaml`: F11 `thread_replies: false`,
   F12 `tool_timeout_s: null` (no deadline: a hung tool hangs the reply forever).
4. `hecklr.yaml`, scripted email scenarios (`smoke`, rules brain, deterministic graders) and guided
   email scenarios (`ci`, luna simulator and judges), email personas.
5. Proof: in-process matrix tests (clean passes all, F11 fails exactly `email_threading`, F12
   fails exactly its tool-timeout scenarios), webhook + real `Sandbox` and GreenMail SMTP/IMAP end
   to end, then `hecklr run --channels email` + `hecklr compare` once WS-wire lands email in
   `hecklr run` (`scripts/email-fault-matrix.sh`). Results in `RESULTS.md`.

## Done

- **Front end** `agent/acme_email`: `EmailDesk` (one conversation per customer address, quoted
  history stripped, threaded replies, `DeadlineTools` on every tool call), `tools.route_world`
  (body `world_url` > `X-Hecklr-World-Url` > base URL + trial id > local), `mailer.yaml`
  (`thread_replies`, `tool_timeout_s`) with overlays. `tests/test_desk.py`, `tests/test_config.py`.
- **Transports**: webhook (`webhook.create_app`: `202`, background reply POST, `/healthz` with
  world sources and replies sent/failed/in flight), mailbox (`mailbox.serve_mailbox`: IMAP
  `UNSEEN`, SMTP replies, worker threads), in process (`InProcessEmailAgent`); CLI
  `agent/serve.py {webhook,mailbox}`. `tests/test_webhook.py` (real `Sandbox`, Hecklr's webhook
  adapter), `tests/test_mailbox.py` (GreenMail in Docker, Hecklr's smtp-imap adapter),
  `tests/test_cli.py`.
- **Faults** `../acme-faults/F11` (`thread_replies: false`), `../acme-faults/F12`
  (`tool_timeout_s: null`): one line of `mailer.yaml` each, with READMEs.
  `test_sc8_email_fault_changes_exactly_one_line_of_mailer_yaml`,
  `test_sc8_email_fault_flips_exactly_its_setting`.
- **Suites** `hecklr.yaml` (webhook) and `hecklr.mailbox.yaml` (smtp-imap): `smoke` (14 scripted),
  `ci` (13 guided core x 3), `full`; 3 email personas; reply deadline 120 s. Every scenario grades `email_threading`
  (`catches-f11`); 6 scripted and 6 guided scenarios inject a hung tool (`catches-f12`).
- **Matrix** (`tests/test_suite.py`, in process): clean 14/14; F11 fails only `email_threading`
  on 14/14; F12 fails exactly the 6 `catches-f12` scenarios as `timeout`.
  `scripts/driver_matrix.py` over real processes and a real sandbox: clean vs clean `no_change`;
  F11 `regression` -1.00 [-1.00, -1.00] p 0.0001; F12 `regression` -0.43 [-0.71, -0.21] p 0.0312
  (RESULTS.md).
- **Live** (luna, `-m live`): 3 passed in 116 s, each under $0.02.
- Numbers: 102 tests (+3 live); 94% line+branch coverage of `agent/`; ruff and `mypy --strict`
  clean (`MYPYPATH=examples/acme-maintenance/agent:examples/acme-email/agent:examples/acme-email/tests
  uv run mypy --strict examples/acme-email/agent examples/acme-email/tests examples/acme-email/scripts`).
- One additive change outside this example: `acme-maintenance/tests/test_faults.py` accepted only
  F1-F6 under `acme-faults/`; it now accepts F1-F12 and still requires F1-F6.

## Done in the review round

- Luna `ci` matrix, first try (45 s deadline, `resolved` everywhere): clean 38.9% [13.7%, 66.5%];
  clean vs clean, F11 (p 0.062) and F12 all INCONCLUSIVE. Causes: the deadline was shorter than a
  clean agent retrying a hung tool, `resolved` cannot pass an email emergency (no handoff), and
  `resolved` failed the fee question while `get_policy` hangs (RESULTS.md 5a).
- Fix: 120 s reply deadline, no judge on emergencies, a policy-hang goal that accepts "the fee
  cannot be checked right now". Luna `ci` clean after the fix: 36/36, 100.0% [75.8%, 100.0%];
  the hung tool was hit in all 18 `catches-f12` trials (RESULTS.md 5b).
- `email-fault-matrix.sh` reads the run id the CLI printed and checks its directory (no stale
  run ids); IMAP logout cannot hide a login error; reproducible test message ids; README section
  "Limits of a reference agent" (webhook trusts `reply_url`, sessions are never evicted).
- Issues #41 (`mypy-all.sh` skips `examples/`) and #42 (`acme-faults/README.md` still says
  F7-F12 come later).

## Done after the re-land (luna proof)

- `DeadlineTools` fails fast for 30 s once a tool missed its deadline, so a clean LLM agent no
  longer spends 5 s per retry of a hung tool; facts-vs-world test for every scenario;
  `email-gas-smell` graded by `tool_called` + a state check on the gas ticket (the
  `handoff_appropriate` judge cannot credit a handoff on email: issue #54); the agent's luna spend
  is logged and capped (`ACME_LLM_BUDGET_USD`).
- Luna `ci` matrix (RESULTS.md 6): clean 39/39 twice, A/A NO CHANGE; F11 REGRESSION −100.0 pts
  (`email_threading`, p < 0.001); F12 REGRESSION −46.2 pts [−69.2, −23.1], p 0.031 (timeouts and
  `resolved` on the 6 `catches-f12` scenarios). Total live spend $0.444. The refund scenario now
  runs (issue #29 fixed on main by #49).

## Next

- Grade email emergencies with `handoff_appropriate` once the email channel can carry a handoff
  (issue #54).

## Open questions

- `scripts/mypy-all.sh` does not type-check `examples/` (issue #41); the command in README.md does.
