# Hecklr

**The crash-test lab for conversational AI agents.** Hecklr throws hundreds of realistic simulated
customers at your agent over **voice, chat and email**, grades every conversation, and tells you
with statistical confidence whether a change made the agent better or worse. Then it gates your CI
on it.

![A one-line prompt edit, caught: hecklr gate reports a regression with its confidence interval and the scenarios that flipped](assets/demo.gif)

## Why

Teams ship prompt, model and tool changes to support agents with no reliable regression signal.
Most eval tools test text in, text out. They miss what actually breaks agents in production: noisy
phone lines, interruptions, slow turn-taking, multi-turn goal drift, manipulative callers, and
plain noise in model outputs that makes a 3-point drop meaningless without error bars.

## What it does

- **Simulates customers** from personas and goals: scripted, adaptive, replayed from real
  transcripts, or adversarial.
- **Calls your agent for real:** audio calls with accents, background noise, phone codecs,
  packet loss, barge-in and silences; chat over HTTP, SSE and WebSocket; email over SMTP/IMAP or
  webhooks.
- **Grades every conversation** with deterministic checks, tool-call and world-state assertions
  against a sandboxed backend, calibrated LLM judges, and voice timing measured from the caller's
  side.
- **Decides with statistics:** confidence intervals on every number, paired significance tests,
  a minimum effect you choose, and a false-alarm rate that is measured, not assumed.
- **Gates CI** with a GitHub Action, a sticky PR comment under 30 lines, JUnit and a
  self-contained HTML report.
- **Closes the loop:** mines production logs into failure clusters and proposed regression
  scenarios, and proposes prompt fixes that are kept only when the suite validates them.

## Start here

<div class="grid cards" markdown>

- **[Quickstart](quickstart.md)**: three commands to a graded run with intervals.
- **[Connect your agent](adapters.md)**: HTTP, OpenAI-compatible, Python, WebSocket, Pipecat,
  LiveKit, Twilio-style streams, SMTP/IMAP, webhooks.
- **[Statistics](concepts/statistics.md)**: the exact methods behind every verdict.
- **[Voice](guides/voice.md)**: turn-taking measured the way a caller hears it.

</div>

## Status

**v1.0 is complete.** All ten success criteria of the
[specification](https://github.com/ebenbaruk/hecklr/blob/main/docs/SPEC.md) are met, three of them
(SC3, SC7, SC10) as the owner scoped them for v1.0
([v1.0 report](https://github.com/ebenbaruk/hecklr/blob/main/docs/milestones/V1.0.md)). Chat,
voice and email, the graders, the statistics, the CI gate, the miner, the fixer, the red-team pack,
the benchmark and the local UI are tested against a reference agent with twelve seeded faults,
each one caught.

The repository is private and unreleased: install from a checkout until the first PyPI release
(see the [quickstart](quickstart.md)). The [roadmap](roadmap.md) lists what happens at launch.
