# Scenarios, personas and worlds

You author three kinds of YAML file. A **scenario** is what a customer wants and what success
looks like; a **persona** is who the customer is and how they behave and sound; a **world** is the
stateful backend your agent's tools act on. Every field is listed in the
[configuration reference](../reference/configuration.md).

## Scenarios

```yaml title="scenarios/reschedule-visit.yaml"
id: reschedule-visit
title: Customer moves a plumbing visit to a morning the week after
channels: [chat, voice]
tags: [core, scheduling]
persona: busy-commuter            # or a list of personas to cross the scenario with
world: acme-starter
goal: Move your plumbing repair visit to a morning (before noon) the following week.
facts:                            # revealed only when the agent asks for them
  customer_id: C-1002
  current_slot: "2026-11-06T14:00"
success:
  - type: tool_called
    tool: reschedule_appointment
    args: { customer_id: C-1002 }
  - type: state
    expr: world.appointments['C-1002'].slot.hour < 12
    description: the new slot is in the morning
  - type: judge
    rubric: resolved
limits: { max_turns: 12, max_duration_s: 300 }
```

A trial passes when **every** success criterion passes. Criteria come in families (deterministic,
tool, state, judge, voice, safety, custom); see [Graders and judges](graders.md).

### Simulation modes

| `mode` | The simulated customer... |
| --- | --- |
| `guided` (default) | is an LLM with a private brief (persona, goal, facts, stop conditions) that pursues the goal adaptively |
| `scripted` | sends `user_turns` in order and calls no model: exactly reproducible, free |
| `replay` | runs like `guided`, with the goal and facts extracted from a real transcript (`hecklr replay`) |
| `adversarial` | runs like `guided`, following the persona's named attack strategy ([red-team pack](../guides/red-team.md)) |

The simulator obeys an **information discipline**: it reveals a fact only when the agent asks for
it or the persona would volunteer it, and it never sees the agent's hidden state. A
**self-consistency guard** checks every simulated turn against the brief (a wrong customer id, a
changed goal) and regenerates contradicting turns. The contradiction rate is printed with an
interval for every run, so a bad simulation never silently becomes an agent failure.

A conversation ends when the goal is achieved, the customer gives up (patience runs out), the agent
hands off to a human or closes the chat, or `max_turns` / `max_duration_s` is reached. The reason
is recorded as the trial's `end_reason`.

### Tool faults

A scenario can make the world misbehave, to test how the agent copes:

```yaml
faults:
  - { tool: get_appointment, kind: timeout }                 # held until the trial ends
  - { tool: find_slots, kind: slow, delay_ms: 4000 }
  - { tool: get_invoice, kind: http_500, probability: 0.5 }  # sampled from the trial seed
  - { tool: get_customer, kind: stale_data, patch: { plan: basic } }
```

## Personas

```yaml title="personas/busy-commuter.yaml"
id: busy-commuter
description: Calls from the car between meetings, terse and short on time, mildly annoyed.
language: en-US
traits: { patience: 0.3, verbosity: 0.2, cooperativeness: 0.7 }
behaviors:
  interrupt_prob: 0.25            # per agent turn, the caller barges in
  backchannel_prob: 0.3           # "mm-hm" during long agent turns; must not stop the agent
  mid_utterance_pause_s: [0.4, 1.2]
  response_gap_ms: [200, 900]
  fillers: true
voice:
  tts: { provider: kokoro, voice: am_michael, rate: 1.1 }
  environment: car_interior       # background noise, mixed at snr_db
  snr_db: 12
  line: g711_mulaw_8k             # telephone band-pass and codec
  packet_loss: 0.02
```

Traits shape the simulator's language and patience on every channel. Behaviors drive the voice
turn-taking controller (barge-in, backchannels, hesitations, silences); chat and email use only the
textual ones. The `voice` profile is required to run a persona on the voice channel: its text-to-
speech voice, background noise and level, room reverb, phone line codec, packet loss and jitter.
See [Voice](../guides/voice.md).

A persona with `adversarial: <strategy id>` attacks the agent instead of cooperating; see the
[red-team pack](../guides/red-team.md).

## Worlds

Agents act through tools, so success is often a state change rather than a sentence. A world is a
small in-memory backend: collections of records plus tools over them. Every trial gets a fresh
copy, so trials never see each other's changes.

```yaml title="worlds/acme-starter.yaml"
id: acme-starter
description: A small home-maintenance company with customers and their booked visits.
collections:
  customers:
    key: id
    records:
      - { id: C-1001, name: Ada Lovelace, postcode: "75001", plan: premium }
      - { id: C-1002, name: Alan Turing, postcode: "69002", plan: basic }
  appointments:
    key: customer_id
    records:
      - { customer_id: C-1002, service: plumbing repair, slot: "2026-11-06T14:00" }
tools:
  - name: get_appointment
    description: The visit booked for a customer.
    params:
      type: object
      properties: { customer_id: { type: string } }
      required: [customer_id]
    op: { kind: get, collection: appointments }
  - name: reschedule_appointment
    description: Move a customer's visit to a new slot, an ISO 8601 local time like 2026-11-12T09:00.
    params:
      type: object
      properties: { customer_id: { type: string }, slot: { type: string } }
      required: [customer_id, slot]
    op: { kind: update, collection: appointments }
```

Tools are collection operations (`get`, `list`, `find`, `create`, `update`, `delete`) or Python
functions (`op: { kind: python, entrypoint: "my_tools:refund" }`). Larger seed data can live in a
JSON, YAML or CSV `seed_file` next to the world.

During a run, Hecklr serves every trial's world on localhost twice:

- as an **MCP server** at `world_mcp_url` (`http://127.0.0.1:<port>/t/<trial id>/mcp`);
- as an **OpenAPI HTTP server** at `world_url` (`POST <world_url>/tools/<name>`, schema at
  `<world_url>/openapi.json`).

Every adapter tells the agent both URLs: the `X-Hecklr-World-Url` and `X-Hecklr-World-Mcp-Url`
headers on HTTP and WebSocket, the WebSocket voice `start` message, Pipecat `request_data`, LiveKit
participant metadata, the email webhook body. Point your agent's tool client at them in test mode
and every call is recorded as a `tool_call` / `tool_result` event, with world snapshots before and
after the trial for `state` assertions.

!!! tip "An agent that cannot be pointed at a sandbox"
    Set `agent.tool_mode: observed`. The agent keeps its own backend and Hecklr reads its tool calls
    from the OpenTelemetry GenAI `execute_tool` spans it emits, joined to trials by the
    `X-Hecklr-Trial-Id` header and sent to the sandbox's OTLP endpoint, so tool assertions
    still work. Details: the `hecklr-world` package README and ADR 0003a.
