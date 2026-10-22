# acme-email: the reference agent by email

The acme-maintenance support agent ([`../acme-maintenance`](../acme-maintenance/README.md))
reachable by email, for Hecklr's email channel and the seeded faults F11 and F12 (SC8). It reuses
the chat agent's brains and files by import (the LLM brain on `openai/gpt-6-luna`, or the
deterministic rules brain) and adds only an email front end (ADR 0012b):

- one conversation per customer address; the brain reads only the new text of each email;
- replies from the address the customer wrote to, in their thread (`In-Reply-To`, `References`,
  `Re:` subject);
- tools at the trial's world sandbox: the webhook body's `world_url`, the `X-Hecklr-World-Url`
  header (HTTP or MIME), `ACME_WORLD_BASE_URL` + the trial id, else a private local copy;
- every tool call under a deadline (`mailer.yaml`, 5 s); a timeout is handled by the brain.

## Run it

```bash
# Webhook (Hecklr's `webhook` email adapter): POST /email -> 202, reply POSTed to reply_url
uv run python examples/acme-email/agent/serve.py --brain rules webhook --port 8100

# Mailbox (Hecklr's `smtp-imap` adapter): a local GreenMail server, then the agent on its inbox
docker compose -f tests/fixtures/email/compose.yaml up -d
uv run python examples/acme-email/agent/serve.py --brain rules mailbox --inbox support@acme.test

# A fault variant: --fault F11 / F12 (mailer.yaml), or F1..F6 (the brain's files)
uv run python examples/acme-email/agent/serve.py --brain rules --fault F12 webhook --port 8100
```

Without `--brain rules` the LLM brain runs on `openai/gpt-6-luna` (needs `OPENAI_API_KEY`;
`--model ollama/mistral:latest` is free). `GET /healthz` (webhook) reports the variant, the
mailer settings, where the tools went (`requests_by_world_source`) and replies sent, failed and in
flight.

Then, with the agent running:

```bash
uv run hecklr run -c examples/acme-email/hecklr.yaml -s smoke --channels email --budget-usd 0
uv run hecklr run -c examples/acme-email/hecklr.mailbox.yaml -s smoke --channels email --budget-usd 0
```

`examples/acme-email/scripts/email-fault-matrix.sh` runs the whole matrix (clean twice, F11, F12)
and compares each run with the clean one. Measured results: [RESULTS.md](RESULTS.md).

## Suites

| Suite | Scenarios | Customers and graders | Use |
| --- | --- | --- | --- |
| `smoke` | `scripted/` (14) | scripted; deterministic graders only | free, exact F11/F12 matrix with the rules brain |
| `ci` | `scenarios/` core (13) x 3 | guided (luna); judges | the real reference agent (LLM brain); clean measured, F11/F12 not yet |
| `full` | `scenarios/` (13) x 5 | guided (luna); judges | more trials; not measured yet |

Every scenario lists `email_threading` (tag `catches-f11`). Scenarios tagged `catches-f12` inject
a hung tool (`faults: [{kind: timeout}]`): `get_policy` during a cancellation (the clean agent
cancels without the policy text) or `create_emergency_ticket` during an emergency (the clean agent
sends the customer to the 24/7 emergency line).

The judges (`resolved`) grade the bookings, moves, plain cancellations, the refund and the
policy-hang cancellations, whose goal accepts that the fee cannot be checked while the policy tool
hangs. Emergencies have no judge: by email the agent cannot hand the customer to the on-call
engineer, so `resolved` failed the clean agent on all of them; `tool_called` and the reply
deadline grade them. The reply deadline (120 s) leaves room for a clean LLM agent to retry a hung
tool before it answers.

## Limits of a reference agent

- `POST /email` trusts its caller: the reply goes to the body's `reply_url`, whatever it is. Do
  not expose the webhook beyond localhost; a real agent replies through its mail provider.
- One conversation per customer address, kept for the life of the process (as in the chat agent):
  a server that runs for days grows without bound. Restart it between runs.
- `/healthz` counts a reply to a trial that already ended (the customer's last "thanks", or F12's
  late answer after the hung tool is released) as `replies_failed`: Hecklr answers `404`.

## Faults

| # | Change (`mailer.yaml`) | Effect | Caught by |
| --- | --- | --- | --- |
| [F11](../acme-faults/F11/README.md) | `thread_replies: false` | replies carry no `In-Reply-To` / `References` | `email_threading` |
| [F12](../acme-faults/F12/README.md) | `tool_timeout_s: null` | a hung tool is awaited forever: no reply | trial `timeout` (email dead air) + `resolved` |

## Tests

```bash
uv run pytest examples/acme-email -n auto --cov=examples/acme-email/agent
MYPYPATH=examples/acme-maintenance/agent:examples/acme-email/agent:examples/acme-email/tests \
  uv run mypy --strict examples/acme-email/agent examples/acme-email/tests
uv run --env-file .env pytest examples/acme-email -m live   # LLM brain on luna, about a cent
```

The GreenMail tests (`test_mailbox.py`) need Docker and are skipped without it.
