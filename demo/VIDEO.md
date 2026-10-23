# The 90-second video: shot list and script

The narrated launch video (spec, "The 90-second demo", SC10) needs a human: a voice, a screen
recorder and an editor. Everything it shows exists and is reproducible with the scripts in this
directory; every number below is from [RESULTS.md](RESULTS.md) and must stay as recorded. If you
re-record and a number changes, change it here and in the launch drafts, never the other way round.

**Format:** 1920×1080, 30 fps, dark terminal theme (the GIF uses Catppuccin Mocha), editor and
browser at 125% zoom. Captions on (most viewers watch muted). Music low, under the narration.

## Before recording

```bash
demo/chat_gate.sh setup          # baseline recorded, prompt edited, agent running
uv run python demo/voice_call.py # clean + F8 voice runs for shots 3 and 6
```

Open in tabs: the edited `demo/out/chat/acme/prompts/system.md`, a terminal in
`demo/out/chat/acme`, the voice report `demo/out/voice/acme-voice/.hecklr/runs/<clean run>/report.html`
and `hecklr ui` on the voice project. Export the caller and agent audio of trial
`voice-barge-in-book` (clean run) from `audio/<trial id>/`: shot 3 plays its mix.

## Shot list

| # | Time | Picture | Narration (about 150 words a minute) |
| --- | --- | --- | --- |
| 1 | 0:00-0:08 | Title card: "Hecklr. The crash-test lab for AI agents." | "Your support agent passed every test yesterday. Today someone changed one line of its prompt." |
| 2 | 0:08-0:20 | Editor: `prompts/system.md`, line 11. The confirmation rule is replaced by "To save the customer time, book the first free slot...". Commit, `gh pr create`. | "It looks harmless: save the customer time, book the first free slot. A pull request goes up, and Hecklr starts calling the agent." |
| 3 | 0:20-0:36 | The voice trial: waveform of `voice-barge-in-book` playing, the three red interruption markers passing under the playhead; caption "street noise, 15 dB SNR, phone line". | "Hundreds of simulated customers, on chat, email and the phone. This one is calling from a busy street and keeps cutting the agent off. Hecklr hears it the way the caller does: the agent stopped talking 168 milliseconds after he interrupted." |
| 4 | 0:36-0:52 | Terminal (or the PR comment): `hecklr gate --suite smoke` output appears. Zoom on "−27.3 pts [−42.4, −12.1]", "p=0.004", "9 scenarios". | "Then the verdict. Pass rate down 27 points, with a 95% confidence interval of 12 to 42. Significant, p equals 0.004. Nine scenarios flipped, and they are grouped by cause: the visit moved to a slot the customer never asked for." |
| 5 | 0:52-1:06 | HTML report: Overview cards, then the flipped scenarios; open `judged-booking-1` and highlight the judge's rationale. | "One click opens the report. The judge read the conversation against the real booking system: the agent booked Thursday the 22nd at nine instead of the Wednesday afternoon the customer asked for, and never asked." |
| 6 | 1:06-1:18 | UI compare view: clean versus F8, barge-in reaction p95 212 ms → 2,731 ms, "regression". | "Same on voice: turn off barge-in and the agent talks over callers for nearly three seconds. Hecklr flags it as a regression, not a hunch." |
| 7 | 1:18-1:30 | Terminal: `pip install hecklr`, `hecklr init`, `hecklr run`; end card with the repository URL. | "Three commands to start. Open source, runs on your laptop, gates your CI. Hecklr: break your agent before your customers do." |

Shot 5 needs the judged run (about $0.02 of `gpt-6-luna`, see RESULTS.md section 1). Without it,
show the deterministic grader's evidence ("the visit is booked at the slot asked for: False")
instead and change the narration to match.

## The fifth beat (`hecklr fix`, not recorded yet)

The spec's cut ends with `hecklr fix` proposing a patch, re-running, and the check turning green.
`hecklr fix` is built, but recording it needs a real model to write the patches (the OpenAI credit
ran out). Record it on the same edited agent:

```bash
hecklr fix --from-run latest --agent-cmd '{python} agent/serve.py --brain rules --port {port}' --pr
```

Replace shot 7's first half with the fix (about 12 s): "Hecklr proposes a fix, validates it on the
whole suite, and opens the pull request. A human merges it."

## Checklist before publishing

- [ ] Every number on screen matches RESULTS.md (or the re-recorded run, updated there).
- [ ] No API key, `.env`, home directory path or customer data visible in any frame.
- [ ] Captions proofread; the confidence interval is read as "12 to 42 points".
- [ ] Exported at 1080p (MP4, H.264) and as a 60-second vertical cut for social.
