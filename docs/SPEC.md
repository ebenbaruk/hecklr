# Hecklr — Product & Technical Specification

Oct 21, 2026 · @Eli Benbaruk · v1.0 draft · [source](https://claude.ai/artifact/YULjJ9FK5tmGSqogANVZsu)

## Name

The project is **Hecklr**: simulated customers who heckle your agent until it breaks, before your real customers do. The name is short, says what the tool does, and is free on both PyPI and npm (checked 2026-10-21).

| Candidate | PyPI | npm | Verdict |
| --- | --- | --- | --- |
| **Hecklr** | free | free | **Pick.** Memorable, tells the story, easy CLI (`hecklr run`) |
| Crowdcall | free | free | Backup. Strong for voice, weaker for chat and email |
| Sparring | free | taken | Good metaphor, npm conflict |
| Gauntlet, Understudy, Rehearse, Stampede, Heckler | taken | taken | Rejected |

Tagline: *The crash-test lab for AI agents.* Before launch, also reserve the GitHub org `hecklr`, the domain `hecklr.dev`, and the PyPI name (publish a 0.0.1 placeholder).

## Executive summary

Hecklr throws hundreds of realistic simulated customers at a conversational agent, over voice, chat and email, before every deploy, and says with statistical confidence whether the change made the agent better or worse.

**The problem.** Teams ship prompt, model and tool changes to support agents with no reliable regression signal. Existing eval tools mostly test text in, text out. They miss what actually breaks agents in production: noisy phone lines, interruptions, slow turn-taking, multi-turn goal drift, manipulative users, and plain noise in LLM outputs that makes a 3-point drop meaningless without error bars.

**What Hecklr does.**

1. **Simulates** customers from personas and goals, or replays real conversations as adaptive simulations.
2. **Calls the agent for real**: WebRTC or SIP audio with accents, background noise, phone codecs, barge-in and silences; chat over HTTP/WebSocket; email over SMTP/IMAP.
3. **Grades** each conversation with deterministic checks, tool-call and world-state assertions, calibrated LLM judges and voice timing metrics.
4. **Decides** with statistics: repeated trials, confidence intervals, paired significance tests and A/A-validated false-positive rates.
5. **Gates** CI with a GitHub Action, a PR comment and a self-contained HTML report.
6. **Closes the loop**: mines production logs into failure clusters and new regression scenarios, then proposes and validates prompt fixes.
7. **Benchmarks** voice support stacks publicly, so the tool has a reason to be talked about.

**Why it matters for the author.** It turns real production experience (sub-800 ms voice, 500-conversation eval gate, 5 deployments) into a public, verifiable artifact that shows real-time audio engineering, statistical rigor and product sense in one repo.

## Goals, non-goals and success criteria

v1.0 is done when every criterion below is met and proven by an automated test or a published artifact, not by a claim in the README.

### Goals

- **G1 Fast start.** Any agent reachable over HTTP, WebSocket, WebRTC, SIP or email can be tested within 15 minutes of `pip install hecklr`.
- **G2 Voice-native.** Simulated callers speak real audio under realistic impairments, and timing is measured at the harness with a ground-truth clock.
- **G3 Trustworthy verdicts.** Every comparison reports effect size, confidence interval and a verdict of improvement, regression or inconclusive.
- **G4 CI-first.** One GitHub Action gates PRs and posts a readable comment.
- **G5 Closed loop.** Production logs become failure clusters and reviewed regression scenarios.
- **G6 Auto-fix.** Candidate patches are generated, validated against the full suite, and opened as PRs only when they win.
- **G7 Public benchmark.** An open dataset and leaderboard for voice support stacks.

### Non-goals for v1

- Not an agent framework. Hecklr tests agents; it never builds them.
- Not a hosted SaaS. Local-first and self-hosted; a cloud offering is out of scope.
- Not a production observability platform. It ingests traces and logs, but does not replace Langfuse-style dashboards.
- No real phone-network (PSTN) calls by default. SIP works against a local or user-provided endpoint.
- No model fine-tuning. Auto-fix edits prompts and tool descriptions only.

### Success criteria (acceptance tests for v1.0)

| ID | Criterion | Target | Verified by |
| --- | --- | --- | --- |
| SC1 | Quickstart on reference agent | `hecklr init` to first report in < 5 min on a laptop | Scripted docs test in CI |
| SC2 | Text throughput | 200 scenarios × 3 trials in < 10 min at concurrency 32 | Perf benchmark |
| SC3 | Voice concurrency | 50 simultaneous simulated calls on an 8-core machine without frame drops | Perf benchmark |
| SC4 | Latency accuracy | Measured response latency within ±20 ms of injected delay | Loopback calibration agent |
| SC5 | False alarms | Regression false-positive rate ≤ 5% at α = 0.05 | 200 A/A comparisons |
| SC6 | Power | Detects a 10-point pass-rate drop with ≥ 80% power on the reference suite | Synthetic power test |
| SC7 | Judge reliability | Cohen's κ ≥ 0.70 vs human labels on every built-in rubric | Labeled calibration set |
| SC8 | Bug catching | Catches 100% of the 12 seeded faults in the reference agent | Fault-injection matrix |
| SC9 | Code quality | ≥ 85% line coverage on core packages, `mypy --strict` clean | CI |
| SC10 | Launch assets | Docs site, 90-second demo video, benchmark with ≥ 6 stacks | Release checklist |

## Users and use cases

The primary user is the engineer who changes the agent; everyone else consumes what Hecklr produces for that engineer.

| Persona | Job to be done | Main surface |
| --- | --- | --- |
| Agent engineer | Know within minutes if a prompt, model or tool change broke something | CLI, HTML report |
| Tech lead / platform owner | Block regressions from reaching production without slowing the team | GitHub Action, PR comment |
| Support ops / QA | Turn yesterday's failed customer conversations into tests | `hecklr mine`, local UI |
| Security / compliance | Prove the agent resists manipulation and does not leak data | Adversarial suite report |
| Researcher / buyer | Compare voice stacks on equal footing | Public benchmark |

### User stories

Each story is an acceptance test owned by the workstream named in the swarm plan.

- **US-01** As an agent engineer, I run `hecklr run` and get a pass rate with a confidence interval per scenario and for the suite.
- **US-02** As an agent engineer, I replay 500 transcripts from production as adaptive simulations, not brittle scripted replays.
- **US-03** As an agent engineer, I open a failed trial and see the transcript, tool calls, world-state diff, judge rationale and, for voice, a waveform with turn events.
- **US-04** As a tech lead, my PR fails only when a regression is statistically significant and above a minimum effect size I configure.
- **US-05** As a tech lead, the PR comment shows what flipped from pass to fail, grouped by cause, in under 30 lines.
- **US-06** As a voice engineer, I see p50/p95/p99 response latency, barge-in reaction time, false interruptions and dead air, sliced by noise level and accent.
- **US-07** As a voice engineer, I see a degradation curve: success rate vs signal-to-noise ratio.
- **US-08** As support ops, I point Hecklr at a log export and get labeled failure clusters with proposed new scenarios to approve.
- **US-09** As security, I run the adversarial persona pack and get a pass/fail report on refund fraud, data leaks and prompt injection.
- **US-10** As an agent engineer, I run `hecklr fix` on a failure cluster and receive a validated patch, or a clear statement that nothing beat the baseline.
- **US-11** As anyone, I label 50 judged trials in the UI and see the judge's agreement with me.
- **US-12** As a researcher, I run `hecklr bench` and reproduce a leaderboard row.

## System architecture

Hecklr has three stages: **Run** places real conversations against the agent, **Judge** turns recordings into a statistically sound verdict, and **Improve** turns production failures into new tests and fixes.

> Diagram: *Hecklr architecture · 13 components in 3 stages* — see the [source spec](https://claude.ai/artifact/YULjJ9FK5tmGSqogANVZsu).

The agent under test is the only external part (dashed); everything it touches during a trial, including its tools, is controlled and recorded by Hecklr.

### Run lifecycle

1. **Load:** parse `hecklr.yaml`, scenarios, personas and world; validate against JSON Schema; fail fast with file and line.
2. **Plan:** expand the trial matrix, derive seeds, estimate cost, refuse to start if the estimate exceeds the budget.
3. **Execute:** for each trial, reset the world, open a session through the adapter, let the simulator and agent converse, record every event with harness timestamps.
4. **Grade:** run deterministic graders first, then LLM judges (cached), then voice metrics.
5. **Aggregate:** compute per-scenario and suite metrics with intervals.
6. **Compare:** pair with the baseline run, compute Δ, p- and q-values, and verdicts; optionally add adaptive trials.
7. **Gate and report:** write reports, set the exit code, post the PR comment.

## Core data model

All entities live in one package, `hecklr-schemas` (Pydantic v2), written and frozen in Wave 0 before any parallel work starts. Authoring formats (scenarios, personas, worlds) are YAML in the user's repo; run outputs go to DuckDB plus Parquet and audio files under `.hecklr/runs/<run_id>/`. Every schema carries `schema_version`, and changes after the freeze require an RFC to the architect agent.

| Entity | Authored or produced | Purpose |
| --- | --- | --- |
| `Scenario` | Authored (YAML) | What the customer wants, what they know, what success looks like |
| `Persona` | Authored (YAML) | Who the customer is and how they behave and sound |
| `World` | Authored (YAML + seed data) | Stateful mock backend the agent's tools act on |
| `Run` | Produced | One execution of a matrix against one agent version |
| `Trial` | Produced | One conversation: scenario × persona × channel × trial index |
| `Event` | Produced | Timestamped item in a trial (utterance, audio span, tool call, interrupt) |
| `Grade` | Produced | One grader's verdict on one trial, with evidence |
| `Comparison` | Produced | Baseline vs candidate statistics and verdict |
| `FailureCluster` | Produced | Group of failed trials or production conversations sharing a root cause |
| `Patch` | Produced | Auto-fix proposal plus its validation results |

### Scenario

```yaml
id: reschedule-boiler-visit
title: Customer moves a boiler maintenance visit
channels: [voice, chat, email]
tags: [scheduling, core]
persona: busy-homeowner          # or a list to cross with
world: acme-maintenance
goal: Move Thursday's visit to next week, mornings only
facts:                            # revealed only when the agent asks
  customer_id: C-1042
  postcode: "92000"
  current_slot: 2026-10-23T09:00
success:
  - type: tool_called
    tool: reschedule_appointment
    args: { customer_id: C-1042 }
  - type: state
    expr: world.appointments['C-1042'].slot.weekday < 5 and world.appointments['C-1042'].slot.hour < 12
  - type: judge
    rubric: resolved
  - type: judge
    rubric: no_hallucination
limits: { max_turns: 24, max_duration_s: 300 }
source: { kind: replay, conversation_id: prod-88213 }   # optional provenance
```

### Persona

```yaml
id: busy-homeowner
description: Calls from a car, short on time, mildly annoyed
language: fr-FR
traits: { patience: 0.4, verbosity: 0.3, cooperativeness: 0.8 }
behaviors:
  interrupt_prob: 0.25        # per agent turn
  backchannel_prob: 0.3
  mid_utterance_pause_s: [0.4, 1.2]
  fillers: true
voice:
  tts: { provider: cartesia, voice: fr-male-2, rate: 1.1 }
  environment: car_interior
  snr_db: 12
  line: g711_mulaw_8k
  packet_loss: 0.02
adversarial: null             # or a strategy id, e.g. refund_social_engineering
```

### Runtime entities (Pydantic, abridged)

```python
class Run(BaseModel):
    id: RunId; created_at: datetime; git_sha: str | None
    agent: AgentRef; config_hash: str; seed: int
    matrix: list[TrialSpec]; budget: Budget; status: RunStatus

class Trial(BaseModel):
    id: TrialId; run_id: RunId
    scenario_id: str; persona_id: str; channel: Channel; index: int; seed: int
    status: Literal["ok", "agent_error", "harness_error", "timeout"]
    started_at: datetime; duration_ms: int; cost_usd: Decimal
    world_before: WorldSnapshot; world_after: WorldSnapshot

class Event(BaseModel):
    trial_id: TrialId; t_ms: int  # harness monotonic clock, sample-accurate for audio
    actor: Literal["user", "agent", "tool", "system"]
    kind: Literal["utterance", "audio_span", "tool_call", "tool_result",
                  "interrupt", "silence", "email", "handoff", "error"]
    payload: dict  # typed per kind via discriminated union in the real code

class Grade(BaseModel):
    trial_id: TrialId; grader: str; grader_version: str
    value: bool | float; passed: bool
    rationale: str | None; evidence: list[EventRef]
    judge_model: str | None; cost_usd: Decimal

class Comparison(BaseModel):
    baseline: RunId; candidate: RunId; metric: str; slice: dict[str, str]
    baseline_value: float; candidate_value: float
    delta: float; ci_low: float; ci_high: float; p_value: float; q_value: float
    verdict: Literal["improvement", "regression", "no_change", "inconclusive"]
```

**Invariants** every workstream relies on: IDs are ULIDs; times are integer milliseconds on the harness monotonic clock; a trial is fully reproducible from (`config_hash`, `seed`, cached model responses); artifacts are append-only.

## Component specifications

Each component is a package with a typed public interface defined in Wave 0; implementations are swapped behind those interfaces, never around them.

### Orchestrator (`hecklr-core`)

- Expands config into a matrix of `TrialSpec` (scenario × persona × channel × trial index), each with a derived seed.
- Runs trials on an `anyio` task group with per-provider rate limiters, a global concurrency cap and a hard cost budget that stops the run cleanly when exceeded.
- Retries harness errors (not agent errors) with exponential backoff; marks trials `harness_error` after 3 attempts and excludes them from statistics with a visible count.
- Response cache: every simulator and judge LLM call is keyed by (model, prompt hash, seed); re-running an unchanged trial costs zero tokens.
- Supports `--adaptive`: after the first pass, allocates extra trials to scenarios whose outcome is uncertain or flipped vs baseline (see Statistics).
- Writes results to DuckDB (`results.duckdb`) and artifacts to Parquet, WAV and JSON.

### Simulator (`hecklr-sim`)

The simulated customer is an LLM agent with a private brief: persona, goal, facts, and stop conditions.

- **Modes:** `scripted` (fixed user turns, for exact reproduction), `guided` (pursues the goal adaptively, the default), `replay` (goal and facts extracted from a real transcript by an LLM, then run as `guided`), `adversarial` (follows a named attack strategy).
- **Information discipline:** facts are revealed only when the agent asks for them or when the persona would volunteer them; the simulator never reads the agent's hidden state.
- **Stop conditions:** goal achieved, goal abandoned (patience exhausted), handoff to human, max turns or duration. The simulator emits a structured `end_reason`.
- **Self-consistency guard:** a lightweight checker flags simulator turns that contradict the brief (wrong customer ID, changed goal) and regenerates them; contradiction rate is reported per run so bad simulations never silently become agent failures.
- **Providers:** thin interface over Anthropic, OpenAI and any OpenAI-compatible endpoint (vLLM, Ollama). A deterministic `FakeLLM` provider ships for tests.

### World sandbox (`hecklr-world`)

Agents act through tools, so success is often a state change, not a sentence.

- A `World` is a YAML schema plus seed data (customers, appointments, orders, policies) loaded into an in-memory store per trial.
- Exposed to the agent as an **MCP server** and as an **OpenAPI HTTP server**, so the agent's real tool code can point at it in test mode.
- Every call is logged as a `tool_call` / `tool_result` event; snapshots before and after feed `state` assertions.
- Fault injection per scenario: tool timeouts, 500 errors, slow responses, stale data, to test agent resilience.
- **Observed mode** for agents that cannot be pointed at the sandbox: tool calls are read from OpenTelemetry GenAI spans the agent emits, joined by the `X-Hecklr-Trial-Id` header.

### Channel drivers (`hecklr-channels`)

| Channel | Transport | Measures |
| --- | --- | --- |
| Chat | HTTP request/response, SSE streaming, WebSocket | Time to first token, time to last token, turn count |
| Voice | WebRTC (aiortc, LiveKit room, Pipecat SmallWebRTC), SIP (optional) | See Voice simulation engine |
| Email | Local SMTP sink + IMAP mailbox, or webhook in/out | Reply time on a simulated clock, threading correctness, attachments |

### Graders (`hecklr-grade`)

| Family | Examples | Notes |
| --- | --- | --- |
| Deterministic | `contains`, `regex`, `max_turns`, `latency_p95_below` | Free, exact, run first |
| Tool | `tool_called`, `tool_not_called`, `args_match`, `call_order` | Read from events |
| State | `state` with a safe expression over `world_before` / `world_after` | Sandboxed evaluator (`simpleeval`), no Python `eval` |
| LLM judge | `resolved`, `no_hallucination`, `policy_compliance`, `tone`, `handoff_appropriate`, `pii_leak` | Structured output: verdict, rationale, evidence event IDs; versioned rubrics; optional 3-judge majority |
| Voice | `response_latency`, `barge_in_reaction`, `false_interruptions`, `dead_air` | Computed from audio timestamps |
| Safety | `no_unauthorized_refund`, `no_cross_customer_data`, `injection_resisted` | Combines state checks and judges |
| Custom | Python entry point `hecklr.graders` | Same `Grader` protocol |

The `no_hallucination` judge receives the world snapshot and tool results, so it checks claims against ground truth rather than plausibility.

### Miner (`hecklr-mine`)

1. **Ingest:** native JSONL, OpenTelemetry GenAI spans, Langfuse and LangSmith exports, and a generic CSV mapper.
2. **Scrub PII:** Presidio plus regex rules plus an LLM pass for free text; scrubbing is consistent (same entity, same placeholder) so conversations stay coherent.
3. **Detect failures:** escalation to human, abandonment, repeated questions, negative sentiment trend, and LLM judges run on the transcript.
4. **Cluster:** embed failure summaries, cluster with HDBSCAN, label each cluster with an LLM ("fails when the customer gives two addresses").
5. **Synthesize:** generate scenario YAML from cluster exemplars, deduplicate against the existing suite by embedding similarity, and queue for human approval in the UI.

### Fixer (`hecklr-fix`)

1. Input: one failure cluster plus the agent files the user declared `editable` in `hecklr.yaml` (system prompt, tool descriptions).
2. Generate N candidate patches (default 5) with an LLM that sees failing transcripts, judge rationales and the current files.
3. **Targeted pass:** run each candidate on the cluster's scenarios; drop those without a significant improvement.
4. **Guard pass:** run survivors on the full suite; drop any with a significant regression on any tag.
5. Output the winning diff, its comparison report and, with `--pr`, a pull request via `gh`.

Guardrails: the fixer can never edit scenarios, graders, rubrics or files outside `editable`; it reports "no candidate beat baseline" honestly; a human always merges.

## Voice simulation engine

The voice engine (`hecklr-voice`) is the differentiator: it places real audio calls and measures turn-taking from the caller's side with a sample-accurate clock, which is how a customer actually experiences latency.

### Outbound path (simulated caller to agent)

1. **Text:** the simulator produces the next utterance, optionally with planned fillers ("uh, hold on") and a planned mid-utterance pause.
2. **Speech:** pluggable TTS. Local and free: Kokoro or Piper. Cloud: Cartesia, ElevenLabs, OpenAI. Output is cached by (text, voice, settings) hash.
3. **Impairments,** applied in this order as a deterministic, seeded chain:
   1. Speaking-rate and pitch jitter per persona
   2. Background noise mixed at a target SNR (car, street, construction site, café, TV, crying child) from a noise library with licenses verified before bundling
   3. Room reverb (impulse responses)
   4. Telephone band-pass and codec: G.711 μ-law 8 kHz, AMR-NB, or low-bitrate Opus
   5. Network: packet loss, jitter and burst loss on the RTP stream
4. **Transport:** 20 ms frames over WebRTC (aiortc, LiveKit room participant, Pipecat SmallWebRTC) or SIP via an optional adapter.

### Inbound path (agent to simulated caller)

1. Agent audio frames are timestamped on arrival and recorded to WAV.
2. **VAD** (Silero, ONNX) segments agent speech into spans.
3. **ASR** (faster-whisper locally, or Deepgram) transcribes the agent so the simulator can respond. Hecklr's own ASR errors are measured on a reference set and reported, so they are never confused with agent errors.

### Turn-taking controller

A state machine, driven by persona parameters, decides when the caller speaks:

- **Normal turn:** waits for agent end-of-speech plus a persona-sampled gap (200–900 ms), then speaks.
- **Barge-in:** with probability `interrupt_prob`, starts speaking at a random point inside the agent's turn.
- **Backchannel:** says "mm-hm" or "okay" during long agent turns, which a good agent must not treat as an interruption.
- **Hesitation:** inserts a mid-utterance pause to test premature endpointing.
- **Silence:** occasionally goes quiet for 5–10 s to test re-engagement prompts.

### Metrics

All timings come from the harness clock (frame counts on the send and receive streams), never from the agent's own logs.

| Metric | Definition | Typical gate |
| --- | --- | --- |
| Response latency | End of caller speech to first agent audio frame; p50, p95, p99 | p95 < 1,000 ms |
| Barge-in reaction | Caller starts speaking over agent to agent audio stopping | p95 < 400 ms |
| False interruption rate | Agent starts speaking during a caller hesitation pause, per 100 caller turns | < 3 |
| Backchannel robustness | Share of backchannels that wrongly stop the agent | < 10% |
| Dead air | Silences > 2 s attributable to the agent, per call | 0 median |
| Talk-over ratio | Seconds of overlap per minute | Reported |
| Degradation curve | Task success rate by SNR bucket (clean, 20, 10, 5, 0 dB) and by accent | Reported, gated on the 10 dB bucket |

### Calibration and self-tests

- **Loopback agent:** echoes a fixed reply after a configurable delay (300, 600, 900 ms) and reacts to barge-in after a known delay. Hecklr must measure both within ±20 ms (SC4). This runs in CI on every change to `hecklr-voice`.
- **Clock drift check:** a 10-minute call must show < 5 ms cumulative drift between send and receive clocks.
- **Concurrency:** 50 simultaneous calls with no frame underruns on an 8-core machine (SC3); the engine runs audio DSP in a process pool, never on the event loop.

## Statistics and reporting

Hecklr never prints a bare number: every metric ships with a confidence interval, and every comparison ends in one of four verdicts whose false-alarm rate is measured, not assumed.

### Metrics

- **Pass rate** per scenario: share of trials where all success criteria pass.
- **Suite pass rate:** mean of per-scenario pass rates (each scenario weighted equally, so a scenario with more trials does not dominate).
- **pass^k:** probability that all k trials of a scenario pass. This is the reliability number customers feel; a 90% pass rate can mean 59% pass^5.
- **pass@k:** probability that at least one of k trials passes (capability ceiling).
- **Latency and cost:** quantiles per trial and per run.

### Intervals

- Per-scenario proportions use the Wilson score interval (correct at small n and near 0% or 100%).
- Suite-level metrics use a **cluster bootstrap** that resamples scenarios, then trials within each scenario, because trials of the same scenario are correlated. Default 10,000 resamples, seeded.
- Latency quantiles use bootstrap intervals on the quantile itself.

### Comparing baseline and candidate

Comparisons are paired by scenario. The suite-level effect is the mean of per-scenario differences:

```latex
\Delta = \frac{1}{S}\sum_{s=1}^{S}\left(\hat{p}^{\,cand}_s - \hat{p}^{\,base}_s\right)
```

- CI for Δ: paired cluster bootstrap. p-value: paired permutation test (sign flips of per-scenario differences).
- Slices (per tag, channel, persona, SNR bucket) are tested too, with Benjamini–Hochberg correction across all slices of a run; reported as q-values.
- **Verdict rule,** with a user-set minimum effect δ (default 3 points) and α (default 0.05):

| Verdict | Condition |
| --- | --- |
| Regression | p < α and Δ < −δ |
| Improvement | p < α and Δ > +δ |
| No change | Whole CI inside \[−δ, +δ\] |
| Inconclusive | Anything else; the report says how many more trials would likely decide it |

### Adaptive trials

With `--adaptive`, after the first pass Hecklr spends a trial budget on scenarios that flipped vs baseline or have wide intervals, using a group-sequential design with an O'Brien–Fleming alpha-spending function so repeated looks do not inflate false alarms. Typical effect: the same decision with 40–60% fewer trials than a fixed design (to be measured and published, not claimed).

### Validating the statistics

- **A/A test** (`hecklr calibrate --aa`): runs the same agent twice and checks that regressions are flagged in ≤ 5% of 200 comparisons (SC5).
- **Power test:** synthetic Bernoulli suites with known effects confirm ≥ 80% power for a 10-point drop on the reference suite size (SC6).
- **Flaky scenario detection:** scenarios whose pass rate sits between 20% and 80% across identical runs are flagged as flaky, so authors fix the scenario or accept it knowingly.

### Judge calibration

- The UI offers a labeling queue; humans label a stratified sample of judged trials per rubric.
- Hecklr reports Cohen's κ, precision and recall of each judge vs humans, with intervals. Rubrics below κ = 0.70 are marked untrusted in reports.
- Changing the judge model triggers a re-score of the labeled set and a drift warning if agreement drops.

### Report formats

| Format | Audience | Content |
| --- | --- | --- |
| Terminal | Engineer | Live progress, then a summary table with intervals |
| Markdown PR comment | Reviewers | Verdict, Δ with CI, flipped scenarios grouped by cause, latency deltas, links; under 30 lines |
| HTML (single self-contained file) | Everyone | Run overview, comparisons, per-trial viewer with transcript, tool calls, world diff, judge rationale, audio player with waveform and turn markers |
| JSON | Machines | Full results with schema version |
| JUnit XML | Any CI | One test case per scenario |

## Developer experience

The whole product is reachable from one CLI and one config file; the UI and the GitHub Action are thin layers over the same commands.

### CLI

| Command | What it does |
| --- | --- |
| `hecklr init` | Interactive setup: detects the agent type, writes `hecklr.yaml`, a starter world, 5 scenarios and 3 personas |
| `hecklr run` | Runs the suite; flags `--suite`, `--channels`, `--trials`, `--adaptive`, `--budget-usd`, `--baseline` |
| `hecklr compare A B` | Statistical comparison of two runs |
| `hecklr gate` | Compare against the stored baseline and exit 0 pass, 1 regression, 2 inconclusive (configurable), 3 harness error |
| `hecklr report RUN` | Builds HTML, Markdown, JSON or JUnit output |
| `hecklr replay FILE` | Converts real transcripts into replay scenarios |
| `hecklr mine LOGS` | Runs the miner and writes clusters plus proposed scenarios |
| `hecklr fix CLUSTER` | Runs the fixer; `--pr` opens a pull request |
| `hecklr calibrate` | `--aa`, `--judges`, `--voice-loopback` self-tests |
| `hecklr bench` | Runs or reproduces the public benchmark |
| `hecklr ui` | Starts the local web UI |
| `hecklr doctor` | Checks keys, audio dependencies, ports and agent reachability |

### Configuration (`hecklr.yaml`)

```yaml
version: 1
agent:
  name: acme-support
  adapters:
    chat:  { type: http, url: http://localhost:8000/chat, stream: sse }
    voice: { type: pipecat, url: http://localhost:7860/offer }
    email: { type: smtp, inbox: support@acme.test }
  editable: [prompts/system.md, tools/descriptions.yaml]   # what the fixer may change
world: worlds/acme-maintenance.yaml
suites:
  ci:      { scenarios: "scenarios/**/*.yaml", tags: [core], trials: 3, channels: [chat] }
  nightly: { scenarios: "scenarios/**/*.yaml", trials: 10, channels: [chat, voice, email], adaptive: true }
  redteam: { personas: "personas/adversarial/*.yaml", trials: 5 }
models:
  simulator: anthropic/claude-sonnet-5-5
  judge:     openai/gpt-4.1
stats: { alpha: 0.05, min_effect: 0.03 }
budget: { usd: 25, concurrency: 32 }
baseline: { store: github-artifacts, branch: main }
```

Model IDs above are examples; any provider and model the provider layer supports can be set.

### GitHub Action (`hecklr/action@v1`)

- Runs `hecklr gate --suite ci` on each PR.
- Fetches the baseline run from the latest successful run on `main` (GitHub artifacts by default; S3 or GCS optional).
- Posts or updates one PR comment and sets a check status; uploads the HTML report as an artifact.
- On `main`, stores the run as the new baseline.
- Caches the LLM response cache between runs to cut cost.

### Python SDK and pytest

```python
import hecklr

@hecklr.agent(channel="chat")
async def my_agent(session: hecklr.Session, message: str) -> str:
    return await support_bot.reply(session.id, message)

# pytest -p hecklr --hecklr-suite=ci   → one test per scenario
```

### Local UI (`hecklr ui`)

FastAPI backend over the DuckDB results; React frontend. Views: runs list, comparison, trial viewer (transcript, tool timeline, world diff, waveform with turn markers via wavesurfer.js), failure clusters, scenario approval queue, judge labeling queue. Read-only on results except labels and approvals.

## Agent adapters

An adapter is the only code that knows how to reach a specific agent; everything upstream talks to the three protocols below, so a new framework is supported by writing one adapter and passing the shared contract test suite.

```python
class ChatTarget(Protocol):
    async def open(self, ctx: SessionContext) -> "ChatSession": ...

class ChatSession(Protocol):
    def send(self, text: str) -> AsyncIterator[AgentChunk]: ...   # streamed tokens + tool events
    async def close(self) -> SessionSummary: ...

class VoiceTarget(Protocol):
    async def connect(self, ctx: SessionContext) -> "AudioLink": ...

class AudioLink(Protocol):
    async def send_frame(self, pcm: bytes, sample_rate: int) -> None: ...   # 20 ms frames
    def frames(self) -> AsyncIterator[TimedFrame]: ...                         # agent audio
    async def hangup(self) -> None: ...

class EmailTarget(Protocol):
    async def deliver(self, msg: EmailMessage, ctx: SessionContext) -> None: ...
    def replies(self, ctx: SessionContext) -> AsyncIterator[EmailMessage]: ...
```

`SessionContext` carries the trial ID, seed, world endpoint URLs (MCP and HTTP) and the correlation header `X-Hecklr-Trial-Id`, so agents that emit OpenTelemetry traces can be joined to trials.

| Adapter | Channel | Priority | Notes |
| --- | --- | --- | --- |
| `http` | Chat | P0 | Templated JSON request/response, SSE or chunked streaming |
| `openai-compatible` | Chat | P0 | Any `/v1/chat/completions` endpoint, including agents behind a proxy |
| `python` | Chat | P0 | In-process callable via `@hecklr.agent` |
| `websocket` | Chat | P1 | Message schema configurable |
| `pipecat` | Voice | P0 | SmallWebRTC offer/answer; used by the reference voice agent |
| `livekit` | Voice | P0 | Joins a room as a participant with a token |
| `webrtc` | Voice | P1 | Generic WHIP-style offer/answer |
| `sip` | Voice | P2 | Via a local SIP user agent; tested against Asterisk in Docker |
| `smtp-imap` | Email | P1 | Local mail sink in Docker for tests |
| `webhook` | Email / chat | P1 | Hecklr exposes an endpoint; the agent posts replies |

**Contract tests:** `hecklr-adapters/tests/contract/` defines one parameterized suite (connect, multi-turn, streaming, barge-in for voice, timeout and error handling). Every adapter must pass it against the matching reference agent before merge.

## Tech stack, repo layout and standards

Python 3.12 monorepo managed by `uv` workspaces, with a TypeScript UI; each package is installable on its own so users only pull audio dependencies when they need voice.

| Layer | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12+, fully typed | Ecosystem of the agent frameworks it tests |
| Packaging | `uv` workspace; PyPI `hecklr` with extras `[voice]`, `[local-voice]`, `[mine]`, `[ui]`, `[all]` | Small default install |
| Concurrency | `anyio` on asyncio; audio DSP in a process pool | Thousands of concurrent text trials, no blocked event loop |
| Schemas | Pydantic v2 | Validation plus JSON Schema export for editors |
| CLI | Typer + Rich | Typed commands, good terminal output |
| HTTP | httpx, FastAPI (world server, UI) | Async, standard |
| LLM providers | Thin in-house layer: Anthropic SDK, OpenAI SDK, OpenAI-compatible | Few dependencies, response caching built in |
| Audio | numpy, scipy, soundfile, aiortc, livekit SDK, Silero VAD (ONNX), faster-whisper | Proven, runs locally |
| Stats | numpy, scipy | No heavy stats framework needed |
| Mining | Presidio, sentence embeddings, HDBSCAN | Standard PII and clustering |
| Storage | DuckDB + Parquet, WAV files | Zero-ops, fast analytics, portable |
| UI | React + Vite + TypeScript + Tailwind, wavesurfer.js | Ships as static files inside the wheel |
| Docs | MkDocs Material, executed snippets | Docs that cannot rot |
| Tooling | ruff, mypy `--strict`, pytest, pytest-xdist, hypothesis, pre-commit | Fast feedback for many parallel agents |
| Distribution | PyPI, Docker image `ghcr.io/hecklr/hecklr`, GitHub Action | Every way teams consume tools |
| License | Apache-2.0 | Permissive, patent grant |

### Repo layout

```text
hecklr/
├── packages/
│   ├── hecklr-schemas/      # Wave 0, frozen interfaces and models
│   ├── hecklr-core/         # orchestrator, storage, cache, budgets, CLI entry
│   ├── hecklr-llm/          # provider layer + FakeLLM
│   ├── hecklr-sim/          # simulated customer
│   ├── hecklr-world/        # world sandbox (MCP + HTTP)
│   ├── hecklr-channels/     # chat and email drivers
│   ├── hecklr-voice/        # audio pipeline, turn-taking, voice metrics
│   ├── hecklr-adapters/     # target adapters + contract tests
│   ├── hecklr-grade/        # graders and rubrics
│   ├── hecklr-stats/        # intervals, tests, adaptive design, calibration
│   ├── hecklr-report/       # terminal, markdown, html, json, junit
│   ├── hecklr-mine/         # ingestion, PII, clustering, synthesis
│   ├── hecklr-fix/          # patch generation and validation
│   └── hecklr/              # meta-package + CLI
├── ui/                      # React app
├── action/                  # GitHub Action
├── examples/
│   ├── acme-maintenance/    # reference agent: chat (FastAPI) + voice (Pipecat) + email
│   ├── acme-faults/         # 12 seeded fault variants
│   └── loopback-voice/      # calibration agent
├── bench/                   # benchmark scenarios, runner, leaderboard site
├── docs/                    # MkDocs + docs/adr/
├── tests/e2e/               # cross-package end-to-end tests
├── CLAUDE.md                # rules for every coding agent
└── .github/workflows/
```

### Engineering standards

- Every public function and class is typed and documented; `mypy --strict` and `ruff` pass with zero ignores added without a comment.
- Unit tests never touch the network: LLM calls go through `FakeLLM` or recorded cassettes; audio tests use generated signals.
- Every random choice takes an explicit seed; no global random state.
- Structured logs (structlog) with `run_id` and `trial_id` on every line; the harness traces itself with OpenTelemetry.
- Architecture decisions are recorded as ADRs in `docs/adr/` (one page each: context, decision, consequences).
- Conventional commits; one workstream per branch; small PRs (< 600 changed lines) unless generated code.

## Testing strategy and quality bar

A testing tool is judged by whether it catches bugs, so Hecklr is tested the same way: a reference agent with 12 deliberately planted faults, which Hecklr must catch every time and never flag in the clean version.

### Test layers

| Layer | Scope | Runs | Cost |
| --- | --- | --- | --- |
| Unit | Each package, `FakeLLM`, synthetic audio | Every push | Free |
| Property | Stats functions and expression evaluator (hypothesis) | Every push | Free |
| Contract | Every adapter against its reference agent | Every PR | Free (fake LLM agent) |
| E2E fake | Full pipeline on `acme-maintenance` with `FakeLLM` for agent, simulator and judges | Every PR | Free, < 5 min |
| Fault matrix | Clean agent + 12 fault variants with real models | Nightly + release | \~ $15 per run, budget-capped |
| Voice calibration | Loopback agent latency and barge-in accuracy | Every PR touching voice | Free |
| Statistical | A/A false-positive rate, power | Nightly | Free (synthetic) |
| Performance | SC2 and SC3 throughput | Weekly + release | Free |
| Docs | Every snippet executed | Every PR touching docs | Free |

### Fault-injection matrix (SC8)

The reference agent `acme-maintenance` handles scheduling, billing questions and emergency callouts for a fictional maintenance company. Each variant in `examples/acme-faults/` changes one thing:

| # | Fault | Must be caught by |
| --- | --- | --- |
| F1 | Prompt drops the instruction to confirm the slot before booking | `tool_called` + `resolved` judge |
| F2 | Tool description for `reschedule` swaps date format to MM/DD | State assertion |
| F3 | Agent invents a cancellation fee | `no_hallucination` judge |
| F4 | Agent reveals another customer's address when asked cleverly | Adversarial persona + `no_cross_customer_data` |
| F5 | Agent grants a refund without verification | `no_unauthorized_refund` |
| F6 | Agent never hands off emergencies (gas smell) to a human | `handoff_appropriate` |
| F7 | +700 ms artificial delay before TTS | Voice response latency gate |
| F8 | Barge-in disabled (agent talks over the caller) | Barge-in reaction metric |
| F9 | VAD end-of-speech threshold set to 150 ms | False interruption rate |
| F10 | ASR model swapped for a weak one | Degradation curve at 10 dB SNR |
| F11 | Email replies lose the thread (no `In-Reply-To`) | Email threading grader |
| F12 | Tool timeout not handled (agent goes silent) | Dead air + `resolved` |

The clean agent must produce no regression verdict against itself (A/A), and each faulty variant must produce a regression verdict on the expected metric.

### Definition of done (every PR)

- [ ] Lint, types, unit, contract and E2E-fake checks green
- [ ] New behavior covered by tests; coverage of touched packages not lower
- [ ] Public API changes documented; ADR added if an architectural choice was made
- [ ] No change to `hecklr-schemas` without an approved RFC
- [ ] Reviewed by the reviewer agent with no unresolved blocking comments

## Agent swarm build plan

The build runs in five waves: one architect agent freezes the interfaces first, then up to six builder agents work in parallel on isolated packages, with an integrator, a reviewer and a red-team agent gating every merge. Parallelism only works because no two builders own the same directory and nobody edits `hecklr-schemas` without an RFC.

### Roles

| Role | Count | Responsibility |
| --- | --- | --- |
| Architect | 1, Wave 0 then on call | Scaffold, `hecklr-schemas`, all Protocols, `FakeLLM`, CI, `CLAUDE.md`; approves or rejects RFCs |
| Builder | Up to 6 at once | Owns one workstream; works in its own git worktree and branch |
| Integrator | 1, continuous | Merges in dependency order, resolves conflicts, keeps `main` green, runs E2E after each merge |
| Reviewer | 1 per PR, fresh context | Reviews against this spec and the DoD; never reviews its own work |
| Red team | 1 per milestone | Tries to break the milestone: bad inputs, concurrency, statistical edge cases; files issues |

### Workstreams

| WS | Workstream | Owns | Depends on | Done when |
| --- | --- | --- | --- | --- |
| 0 | Foundation | `hecklr-schemas`, repo, CI, `hecklr-llm` (FakeLLM + providers) | none | Schemas frozen, CI green on empty packages, `FakeLLM` deterministic |
| 1 | Orchestrator & storage | `hecklr-core` | 0 | Matrix expansion, budgets, cache, DuckDB writes; 1,000 fake trials in < 60 s |
| 2 | Simulator | `hecklr-sim` | 0 | 4 modes; contradiction guard; replay extraction tested on 20 transcripts |
| 3 | World sandbox | `hecklr-world` | 0 | MCP + HTTP servers, snapshots, fault injection |
| 4 | Graders | `hecklr-grade` | 0 | All deterministic, tool, state graders; 6 judge rubrics with versioned prompts |
| 5 | Statistics | `hecklr-stats` | 0 | Wilson, cluster bootstrap, permutation, BH, verdicts; SC5 and SC6 pass |
| 6 | Chat channel & adapters | `hecklr-channels` (chat), `hecklr-adapters` (chat) | 0 | P0 chat adapters pass contract suite |
| 7 | Reports & CLI & Action | `hecklr-report`, `hecklr` CLI, `action/` | 1, 5 | All 5 formats; `gate` exit codes; Action posts a PR comment on a test repo |
| 8 | Reference agent (chat) + faults F1–F6 | `examples/acme-*` | 3 | Clean agent and 6 variants run; Hecklr catches F1–F6 |
| 9 | Voice engine | `hecklr-voice` | 0 | Outbound and inbound paths, impairments, turn-taking, metrics |
| 10 | Voice transports | `hecklr-adapters` (voice) | 9 | Pipecat and LiveKit adapters pass contract suite |
| 11 | Voice reference + loopback + F7–F10 | `examples/` voice parts | 9, 10 | SC3, SC4 pass; F7–F10 caught |
| 12 | Email channel + F11, F12 | `hecklr-channels` (email) | 1, 3 | SMTP/IMAP in Docker; F11–F12 caught |
| 13 | Miner | `hecklr-mine` | 2, 4 | 5 stages; clusters on a 1,000-conversation synthetic log set |
| 14 | Adversarial pack | `personas/adversarial/`, safety graders | 2, 4 | 8 attack strategies; F4, F5 caught |
| 15 | Fixer | `hecklr-fix` | 4, 5, 13 | Fixes F1 automatically on the reference agent; guard pass rejects a planted bad patch |
| 16 | UI | `ui/`, UI API | 1, 7 | All views; labeling and approval queues work |
| 17 | Benchmark | `bench/` | 11 | ≥ 6 stacks run reproducibly; static leaderboard site |
| 18 | Docs & launch | `docs/`, README, demo | all | SC1, SC10 pass |

### Waves

1. **Wave 0 (serial):** WS0 by the architect. Output: frozen schemas and Protocols, empty packages with passing CI, `CLAUDE.md`. Gate: human review of schemas (this is the one place you should read code carefully).
2. **Wave 1 (parallel, 6 builders):** WS1, WS2, WS3, WS4, WS5, WS6. Then WS7 and WS8 as builders free up. Gate M1: `hecklr run` works end to end on the chat reference agent; F1–F6 caught.
3. **Wave 2 (parallel, 4 builders):** WS9, WS10, WS11, WS12. Gate M2: voice SC3, SC4, F7–F12 caught. **Public launch point.**
4. **Wave 3 (parallel, 3 builders):** WS13, WS14, WS16. Gate M3: production loop and red-team suite usable from the UI.
5. **Wave 4 (parallel, 3 builders):** WS15, WS17, WS18, then a hardening sprint where the red team and reviewer close every P0 and P1 issue. Gate v1.0: all success criteria SC1–SC10 green.

### Orchestration rules

- Each builder starts from a prompt built from the template in the appendix: its workstream row, the relevant spec sections, its owned paths and its Done criteria.
- Builders work in separate git worktrees on `ws-<n>-<slug>` branches and open PRs to `main`; only the integrator merges.
- A builder that needs an interface change stops and writes an RFC in `docs/rfc/`; the architect answers before work continues on that part.
- Builders keep a `PROGRESS.md` in their package (what is done, what is next, open questions) so a fresh agent can resume after a context reset.
- Integration happens at least daily; no branch lives more than 2 days without merging something.
- Real model calls in tests are capped by `HECKLR_TEST_BUDGET_USD`; builders use `FakeLLM` by default.

## Milestones, demo and launch

Hecklr goes public at M2, when voice works and 12 of 12 seeded faults are caught; everything after that ships as visible progress on a live project.

> Diagram: *Build roadmap · 5 waves, 5 gates* — see the [source spec](https://claude.ai/artifact/YULjJ9FK5tmGSqogANVZsu).

A wave closes only when the integrator's milestone report shows evidence for every gate criterion.

### The 90-second demo (recorded at M2)

1. A developer edits one line in `prompts/system.md` and opens a PR.
2. The Hecklr check starts; the video cuts to one simulated call: a caller on a noisy line interrupts the agent mid-sentence.
3. The PR comment appears: resolution down 7 points, 95% CI \[−11, −3\], regression, 9 scenarios flipped, cluster "fails on rescheduling requests".
4. One click opens the HTML report: waveform, the interruption marked, the judge's rationale.
5. (Post-M3 cut) `hecklr fix` proposes a patch, re-runs, check turns green.

Numbers in the demo must come from a real run, recorded as-is.

### Launch checklist (M2)

- [ ] README with the demo GIF in the first screen, 3-command quickstart, comparison table vs other eval tools
- [ ] Docs site live with quickstart, concepts, adapters, statistics explainer
- [ ] Blog post: "Your voice agent is slower than you think: measuring turn-taking from the caller's side"
- [ ] Show HN post, X/Twitter thread, LinkedIn post with the video
- [ ] Posts in the Pipecat and LiveKit communities with a working example for each
- [ ] Issues labeled `good first issue` (at least 10) and a CONTRIBUTING guide
- [ ] Resume line ready: *Creator of Hecklr, an open-source crash-test lab for conversational agents (voice, chat, email; statistical CI gating)*, updated with stars and users after 30 days

## Risks and open questions

The biggest risk is not technical: it is building 80% of everything and finishing nothing, so the launch is pinned to M2 and later waves are optional for the launch.

| Risk | Impact | Likelihood | Mitigation |
| --- | --- | --- | --- |
| Scope creep across 18 workstreams | Nothing ships | High | Launch at M2; Waves 3–4 only after launch; cut WS17 before cutting quality |
| Swarm agents drift from the spec or each other | Broken integration | High | Frozen schemas, owned paths, contract tests, daily integration, fresh-context reviewer |
| Simulator errors counted as agent failures | Users stop trusting verdicts | Medium | Contradiction guard, contradiction rate in every report, A/A tests |
| LLM judges disagree with humans | Wrong gates | Medium | κ calibration, untrusted-rubric marking, deterministic graders first |
| Real-time audio is hard to get right in Python | Bad latency numbers | Medium | Process pool for DSP, frame-count clock, loopback calibration in CI |
| Cost of real-model test runs | Budget overrun | Medium | `FakeLLM` by default, response cache, hard budget caps |
| Noise or voice datasets with restrictive licenses | Legal issue at launch | Low | License check before bundling; download-on-demand for anything non-permissive |
| Overlap with existing eval tools | "Why not X?" at launch | Medium | Lead with voice realism and statistics; publish an honest comparison page |
| Employer IP concerns | Project at risk | Low | Clean-room build from public knowledge, synthetic data only, contract checked |

### Open questions

- Which voice stacks go in the launch benchmark (needs API access and budget per provider)?
- Is SIP support worth it for v1, or deferred to v1.1?
- Should email simulate time (fast-forward the clock) or run in real time with short timeouts?
- Is the GitHub org `hecklr` and the domain `hecklr.dev` available? Not yet checked.
- What total model budget is acceptable for the build (swarm tokens plus test runs)?

## Appendix: CLAUDE.md and agent prompts

Copy these verbatim into the repo and into each Claude Code session; they encode the rules that keep parallel agents from breaking each other's work.

### `CLAUDE.md` (repo root)

```markdown
# Hecklr — rules for coding agents

Hecklr is a crash-test lab for conversational agents: simulated customers over voice, chat and email, statistical verdicts, CI gating. The full spec is in docs/SPEC.md. Read the sections named in your task before writing code.

## Non-negotiables
- Never edit packages/hecklr-schemas unless your task says you are the architect. Need a change? Write docs/rfc/NNNN-title.md and stop work on that part.
- Only edit the paths your workstream owns (listed in your task). Anything else: open an issue.
- Every random choice takes an explicit seed. No global random state.
- Unit tests never use the network. Use FakeLLM or recorded cassettes. Real model calls only in tests marked @pytest.mark.live, within HECKLR_TEST_BUDGET_USD.
- Times are integer milliseconds on the harness monotonic clock. IDs are ULIDs.
- Audio DSP never runs on the event loop.

## Before every commit
- uv run ruff check . && uv run ruff format --check .
- uv run mypy --strict packages/<your-package>
- uv run pytest packages/<your-package> -n auto
- uv run pytest tests/e2e -m fake   (if you touched anything the pipeline uses)

## Working style
- Write the test first for any behavior in the spec's success criteria or user stories; cite the ID (SC4, US-06) in the test name.
- Keep packages/<your-package>/PROGRESS.md current: done, next, open questions.
- Small PRs (< 600 changed lines). Conventional commits. Branch ws-<n>-<slug>.
- Record architectural choices as docs/adr/NNNN-title.md.
- If the spec is ambiguous, choose the simplest option that satisfies the success criteria, note it in PROGRESS.md, and continue.
```

### Builder prompt template

```markdown
You are the builder for workstream WS<n>: <name>.

Read first: CLAUDE.md, then docs/SPEC.md sections: <sections>.
You own: <paths>. Do not edit anything else.
Interfaces you implement or consume (frozen): <Protocols and models>.
Depends on: <workstreams>; use their public APIs only, or FakeLLM / fixtures if not merged yet.

Done when:
<Done criteria from the workstream table>
<relevant success criteria and user stories>

Process:
1. Read the spec sections and existing code in your paths. Write a short plan in PROGRESS.md.
2. Build test-first, in small commits. Run the full pre-commit checks before each push.
3. Open a PR per coherent slice. Respond to reviewer comments.
4. When all Done criteria pass, update PROGRESS.md with evidence (test names, numbers) and stop.
If you need an interface change, write an RFC and work on other parts while you wait.
```

### Reviewer prompt

```markdown
You are the reviewer. You did not write this code. Review PR #<n> against:
1. docs/SPEC.md sections for WS<n> and the Done criteria.
2. CLAUDE.md non-negotiables.
3. Correctness first: concurrency bugs, unseeded randomness, clock misuse, untested branches, silent exception swallowing, statistical mistakes.
Run the tests yourself. Mark each comment BLOCKING or NIT. Approve only with zero BLOCKING comments.
```

### Integrator prompt

```markdown
You are the integrator. Merge approved PRs into main in dependency order (see the workstream table). After each merge run the full test suite and tests/e2e -m fake. If main breaks, revert the merge and send the failure to the PR's builder. At the end of each wave, run the milestone gate and write docs/milestones/M<n>.md with evidence for every gate criterion.
```

### Red-team prompt

```markdown
You are the red team for milestone M<n>. Try to make Hecklr give a wrong verdict or crash: malformed YAML, huge suites, agents that hang or flood output, judges that return garbage, A/A runs with tiny samples, audio with clipping or silence, clock skew. File each finding as an issue with a reproduction and severity P0–P3. Do not fix anything yourself.
```
