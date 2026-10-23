# Security policy

## Supported versions

Hecklr is pre-release. Security fixes go to `main` and to the latest release once releases
exist.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Report it privately through
GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository (**Security → Report a vulnerability**). Include what you found, how to
reproduce it, and the impact you expect. You will get an acknowledgement within 5 working days and
a plan or a fix within 30 days; we will credit you in the advisory unless you prefer otherwise.

## What is in scope

Hecklr runs other people's agents against local servers it starts, and reads files and logs that
may contain customer data. Reports we especially want:

- Escaping the state-expression sandbox (`state` criteria run in simpleeval, never Python `eval`).
- A world sandbox, webhook listener or the local UI reachable beyond 127.0.0.1 by default, or the
  UI writing anything other than labels and review decisions.
- Secrets (model API keys, LiveKit or SMTP credentials) written to logs, reports, run artifacts or
  the response cache. Hecklr reads secrets only from the environment; config fields ending in
  `_env` hold variable names, never values.
- Personal data surviving the miner's scrubbing in a way the documentation says it should not
  (note that person names are only scrubbed with `--presidio` or `--llm-scrub`).
- The GitHub Action exposing a token or running untrusted pull request code with write
  permissions.

Out of scope: the behavior of the agent under test (finding an agent's weaknesses is Hecklr's job),
the seeded faults in `examples/acme-faults/`, and denial of service from a configuration you wrote
yourself.
