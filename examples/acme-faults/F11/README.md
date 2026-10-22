# F11: email replies lose the thread

**Change.** `mailer.yaml`, the email front end's settings (`examples/acme-email`), turns threading
off:

```diff
-thread_replies: true
+thread_replies: false
```

**Effect.** The email agent's replies carry no `In-Reply-To` and no `References`. The subject
still reads `Re: <subject>` and the text is unchanged, so a reader of the text alone sees nothing
wrong; a mail client files every reply as a new conversation.

**Must be caught by** the `email_threading` grader (spec table). Every acme-email scenario lists
it as a success criterion (tag `catches-f11`), so every trial in which the agent replies fails.
Nothing else changes: on the scripted `smoke` suite with the rules brain, `email_threading` is the
only failing grade of every F11 trial
(`tests/test_suite.py::test_sc8_f11_fails_threading_on_every_scripted_scenario`).

F11 is an email fault: the chat agent refuses this overlay (it holds no file the chat agent has).

```bash
uv run python examples/acme-email/agent/serve.py --fault F11 webhook --port 8100
```
