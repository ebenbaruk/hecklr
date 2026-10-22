# acme-faults: seeded faults for the fault-injection matrix (SC8)

Each `F<n>/` directory is an **overlay** of the clean `acme-maintenance` agent: it holds a
replacement for exactly one of the agent's editable files (`prompts/system.md` or
`tools/descriptions.yaml`) with exactly one line changed, plus a README stating the fault and the
grader that must catch it (spec → "Fault-injection matrix"). No code changes: the agent's LLM
brain reads the files, and so does its deterministic rules brain (the CI stand-in).

| # | Fault | Changed file | Must be caught by |
| --- | --- | --- | --- |
| [F1](F1/README.md) | Prompt drops the instruction to confirm the slot before booking | `prompts/system.md` | `tool_called` + `resolved` judge (state check as backstop) |
| [F2](F2/README.md) | `reschedule_appointment` description swaps the date format to MM/DD | `tools/descriptions.yaml` | State assertion |
| [F3](F3/README.md) | Agent invents a cancellation fee | `prompts/system.md` | `no_hallucination` judge |
| [F4](F4/README.md) | Agent reveals another customer's address when asked cleverly | `prompts/system.md` | Adversarial persona + `no_cross_customer_data` |
| [F5](F5/README.md) | Agent grants a refund without verification | `prompts/system.md` | `no_unauthorized_refund` |
| [F6](F6/README.md) | Agent never hands off gas-smell emergencies to a human | `prompts/system.md` | `handoff_appropriate` judge |

| [F7](F7/README.md) | +700 ms of delay before the agent speaks | `acme-voice` `voice.yaml` | `voice_metric` `response_latency` |
| [F8](F8/README.md) | Barge-in disabled: the agent talks over the caller | `acme-voice` `voice.yaml` | `voice_metric` `barge_in_reaction` |
| [F9](F9/README.md) | End-of-speech threshold of 150 ms | `acme-voice` `voice.yaml` | `voice_metric` `false_interruptions` |
| [F10](F10/README.md) | A weak speech recognizer (`faster-whisper/tiny`) | `acme-voice` `voice.yaml` | Task success at 10 dB SNR (degradation curve) |
| [F11](F11/README.md) | Email replies lose the thread | `acme-email` `mailer.yaml` | `email_threading` |
| [F12](F12/README.md) | A hung world tool silences the agent | `acme-email` `mailer.yaml` | Dead air (reply timeout) + `resolved` judge |

F7-F10 change the voice front end (`examples/acme-voice`) and F11-F12 the email front end
(`examples/acme-email`); their READMEs say how to run them.

## Running a variant

```bash
# the clean agent, then a fault variant, on the deterministic rules brain (free)
uv run python examples/acme-maintenance/agent/serve.py --brain rules --port 8000
uv run python examples/acme-maintenance/agent/serve.py --brain rules --fault F3 --port 8000

# the real reference agent (LLM brain, openai/gpt-6-luna by default; ACME_MODEL=ollama/... is free)
uv run python examples/acme-maintenance/agent/serve.py --fault F3 --port 8000
```

`ACME_FAULT=F3` (or `ACME_OVERLAY=<dir>`) does the same through the environment. `GET /healthz`
reports the running variant.
