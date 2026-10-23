# How Hecklr compares

"Why not X?" is a fair question; there are good tools in this space. This page compares Hecklr
with four of them on what Hecklr is built for: testing a conversational agent end to end, over
voice, chat and email, and gating changes on statistics. It is not a ranking: each tool is built
for a somewhat different job, and several do things Hecklr does not.

Every claim about another tool comes from its public documentation, checked on **2026-10-23**
(links below). Tools change quickly; if something here is out of date or unfair, please open an
issue and it will be corrected.

| | Hecklr | [promptfoo](https://www.promptfoo.dev/docs/intro/) | [DeepEval](https://deepeval.com/docs/getting-started) | [Coval](https://docs.coval.ai/) | [Cekura](https://docs.cekura.ai/) |
| --- | --- | --- | --- | --- | --- |
| License and hosting | Apache-2.0, runs locally | MIT, runs locally (hosted option) | Apache-2.0, runs locally (Confident AI platform optional) | Commercial, hosted | Commercial, hosted |
| Multi-turn simulated users | Yes: scripted, guided, replay, adversarial | Yes ([simulated user](https://www.promptfoo.dev/docs/providers/simulated-user/)) | Yes ([ConversationSimulator](https://deepeval.com/docs/conversation-simulator)) | Yes, voice and text | Yes, voice and text |
| Calls a voice agent with real audio | Yes: WebSocket (PCM, Twilio, Pipecat), Pipecat WebRTC, LiveKit | Text-focused | Yes ([voice mode](https://deepeval.com/docs/conversation-simulator): speech, background noise, interruptions) | Yes | Yes, including telephony and SIP |
| Voice timing metrics | Latency, barge-in reaction, false interruptions, backchannels, dead air, talk-over; clock calibrated to ±20 ms in CI | — | Latency captured per turn | Latency, time to first audio, interruption rate, pauses, and many audio-quality metrics | Latency percentiles, interruption and stop-time metrics |
| Email channel | Yes: SMTP/IMAP and webhook | — | — | — | — |
| Stateful tool sandbox with state assertions | Yes: a world served over MCP and HTTP per trial | Output assertions, including custom code | Tool-correctness metrics on traces | API-state checks and trace-based tool-call metrics | Custom workflow metrics |
| Gate decision | Paired significance test, CI on the change, minimum effect, BH-corrected slices; A/A false-alarm rate measured | Pass-rate threshold | Metric thresholds | Fails when any simulation scores a chosen metric value | Every tagged run must pass, with optional repetitions |
| CI integration | GitHub Action with sticky PR comment, JUnit, exit codes | GitHub Action with PR comment, CI guides | pytest integration | GitHub Action, CLI | GitHub Action, GitLab CI |
| Production logs to new tests | `hecklr mine`: scrub, cluster, propose scenarios | — | Via Confident AI | Production monitoring | Production monitoring, call-to-scenario |
| Red teaming | 8 customer-support attack strategies | Extensive: 50+ vulnerability types, compliance frameworks | Via DeepTeam | — | — |

"—" means we found no such feature in the tool's documentation, not that it is impossible.

## Where Hecklr is different

- **Statistics are the product.** Agents are noisy; a 3-point drop on a small suite is usually
  noise. Hecklr pairs runs by scenario, reports every number with a confidence interval, and only
  fails a check on a significant change larger than the effect you care about. Its false-alarm rate
  and power are measured in CI ([Statistics](concepts/statistics.md)).
- **Voice timing from the caller's side.** Latency is measured in audio frames at the harness, the
  way a caller hears it, and the instrument itself is calibrated against a loopback agent with known
  timing.
- **State, not just text.** Every trial gets its own copy of a mock backend, so "the visit was
  moved to a weekday morning" is checked against the world, not inferred from what the agent said.
- **Local and open.** Your conversations, recordings and results stay on your machine, in DuckDB
  and Parquet files you can query.

## Where another tool may fit better

- **Broad LLM security scanning:** promptfoo's red teaming covers far more vulnerability classes
  and compliance frameworks than Hecklr's eight support-focused strategies.
- **Single-turn and RAG metrics:** DeepEval and promptfoo have large libraries of metrics for
  retrieval, faithfulness and single responses; Hecklr grades whole conversations.
- **A managed service:** Coval and Cekura run the calls for you, including over the phone network,
  with dashboards and production monitoring. Hecklr is local-first and does not place PSTN calls.

## Sources

- promptfoo: [simulated user](https://www.promptfoo.dev/docs/providers/simulated-user/),
  [CI/CD integration](https://www.promptfoo.dev/docs/integrations/ci-cd/),
  [red teaming](https://www.promptfoo.dev/docs/red-team/),
  [repository and license](https://github.com/promptfoo/promptfoo).
- DeepEval: [conversation simulator and voice mode](https://deepeval.com/docs/conversation-simulator),
  [repository and license](https://github.com/confident-ai/deepeval).
- Coval: [simulated conversations](https://docs.coval.ai/concepts/simulations/overview),
  [metrics](https://docs.coval.ai/concepts/metrics/overview),
  [GitHub Action](https://github.com/coval-ai/coval-github-action).
- Cekura: [infrastructure suite and CI](https://docs.cekura.ai/documentation/guides/testing-agents/infrastructure-suite),
  [Pipecat integration](https://docs.pipecat.ai/pipecat/evals/platforms/cekura).
