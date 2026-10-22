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
  `ci` (13 guided core x 3), `full`; 3 email personas. Every scenario grades `email_threading`
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

## Next

- After WS-wire merges email into `hecklr run`: `scripts/email-fault-matrix.sh` (smoke, rules brain,
  $0), then `SUITE=ci BRAIN=llm BUDGET_USD=0.5` on luna; record both in RESULTS.md.

## Open questions

- Issue #29 (simulator consistency guard rejects guided refund/invoice turns) may turn
  `email-refund-double-charge` into harness errors on real models; it is excluded from statistics,
  not counted against the agent.
- `scripts/mypy-all.sh` does not type-check `examples/` (also noted by WS8); the command above does.
