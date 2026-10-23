# Graders and judges

Each entry of a scenario's `success` list is graded on every trial, and a trial passes when all of
them pass. Graders run in order of cost: free deterministic checks first, then cached LLM judges,
then voice metrics. Every grade records its evidence (the event ids it looked at) and, for judges,
a rationale you can read in the report.

| Family | Criteria | Notes |
| --- | --- | --- |
| Deterministic | `contains`, `not_contains`, `regex`, `max_turns`, `latency_p95_below` | Free, exact |
| Tool | `tool_called`, `tool_not_called`, `args_match`, `call_order` | Read from the trial's `tool_call` events |
| State | `state` | A safe expression over the world before and after the trial |
| LLM judge | `judge` with a rubric: `resolved`, `no_hallucination`, `policy_compliance`, `tone`, `handoff_appropriate`, `pii_leak` | Structured verdict, rationale and evidence; versioned rubrics; optional 3-judge majority |
| Voice | `voice_metric`: `response_latency`, `barge_in_reaction`, `false_interruptions`, `backchannel_robustness`, `dead_air`, `talk_over_ratio` | Computed from the harness's audio timestamps |
| Email | `email_threading` | `In-Reply-To`, `References`, subject |
| Safety | `no_unauthorized_refund`, `no_cross_customer_data`, `injection_resisted`, plus the red-team pack's custom graders | State checks combined with judges |
| Custom | `custom` with `name` and `params` | Your own grader, registered as a Python entry point |

## Tool and state criteria

```yaml
success:
  - { type: tool_called, tool: reschedule_appointment, args: { customer_id: C-1042 } }
  - { type: args_match, tool: reschedule_appointment, patterns: { slot: '\d{4}-\d{2}-\d{2}T\d{2}:\d{2}' } }
  - { type: call_order, tools: [verify_customer, issue_refund] }
  - type: state
    expr: world.appointments['C-1042'].slot.weekday < 5 and world.appointments['C-1042'].slot.hour < 12
    description: moved to a weekday morning
```

`tool_called` asks whether one good call exists; `args_match` asks whether **every** call is
well-formed. State expressions run in a sandboxed evaluator ([simpleeval](https://github.com/danthedeckie/simpleeval)),
never Python's `eval`. Names: `world` (the world after the trial), `world_before`, and the
scenario's `facts`. ISO 8601 strings expose `year`, `month`, `day`, `hour`, `minute`, `weekday`
(Monday is 0) and `date`. Functions: `len any all min max sum abs round sorted int float str bool
keys values`.

## LLM judges

A judge reads the transcript and answers with a structured verdict, a rationale and the ids of the
events that support it. Each rubric is a versioned prompt (its version is a hash of the prompt),
so a changed rubric is never silently compared with an old one. `n_judges: 3` asks three times and
takes the majority.

`no_hallucination` receives the world snapshot and every tool result, so it checks the agent's
claims against ground truth rather than plausibility: an invented cancellation fee fails even when
it sounds reasonable.

Judges are only as good as their agreement with people, so Hecklr measures it. Label a sample of
judged trials in the [UI](../guides/ui.md) (or a `labels.jsonl`), then:

```bash
hecklr calibrate --judges --labels .hecklr/labels.jsonl --run latest
```

This reports Cohen's κ, precision and recall (with "fail" as the positive class) for every rubric
against your labels, each with an interval. A rubric below κ = 0.70 is marked **untrusted** in
reports. Changing the judge model re-scores the labeled set and warns when agreement drops.

## Voice metrics

All timings come from the harness clock, measured in audio frames on the send and receive
streams: what a caller actually hears, never what the agent logs.

| Metric | Definition | Typical gate |
| --- | --- | --- |
| `response_latency` | End of caller speech to first agent audio frame; p50, p95, p99 | p95 < 1,000 ms |
| `barge_in_reaction` | Caller starts talking over the agent to agent audio stopping | p95 < 400 ms |
| `false_interruptions` | Agent starts speaking inside a caller's hesitation pause, per 100 caller turns | < 3 |
| `backchannel_robustness` | Share of "mm-hm"s that wrongly stop the agent | < 10% |
| `dead_air` | Silences over 2 s attributable to the agent, per call | 0 |
| `talk_over_ratio` | Seconds of overlap per minute | reported |

```yaml
success:
  - { type: voice_metric, metric: response_latency, below: 1000, quantile: p95 }
  - { type: voice_metric, metric: barge_in_reaction, below: 400 }
```

See [Voice](../guides/voice.md) for how these are measured and calibrated.

## Custom graders

A custom grader implements the same `Grader` protocol as the built-in ones; register a
zero-argument factory for it under the `hecklr.graders` entry-point group of your package:

```toml
[project.entry-points."hecklr.graders"]
refund_within_policy = "my_graders:RefundWithinPolicy"
```

```yaml
success:
  - { type: custom, name: refund_within_policy, params: { max_eur: 200 } }
```

## Grade outcomes

A grade is `ok` (it decided pass or fail), `skipped` (not applicable, e.g. a voice metric on a chat
trial) or `error` (no verdict could be reached, e.g. a judge answered with garbage). From the
grades, each trial gets one verdict:

- an agent error or a timeout is a failed trial, whatever the grades say;
- a failing `ok` grade fails the trial, even if another grader errored;
- a trial whose criteria all passed except some that errored is **undecided**: it is left out of
  the statistics and counted next to the harness errors, because calling it a pass or a fail would
  be a guess;
- otherwise the trial passes when every `ok` criterion grade passed.
