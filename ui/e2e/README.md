# Browser check of `hecklr ui` on a real store

`npm run e2e` drives a running `hecklr ui` in Chromium (Playwright). It needs a store that holds
two runs of one suite (an older clean baseline and a candidate with failures), a run with judge
grades, and miner output with at least two open proposals. Everything below is free (rules brain,
`fake/` models). Use your own ports.

```bash
cp -R examples/acme-maintenance /tmp/acme          # a scratch copy: the check writes labels,
                                                   # decisions and scenarios/mined/
# point /tmp/acme/hecklr.yaml's chat url at :18431 and set models to fake/customer, fake/judge
uv run python examples/acme-maintenance/agent/serve.py --brain rules --port 18431 &
uv run hecklr run -c /tmp/acme/hecklr.yaml --suite smoke             # clean baseline
uv run hecklr run -c /tmp/acme/hecklr.yaml --suite ci --trials 1     # judge grades
# restart the agent with --fault F1, then:
uv run hecklr run -c /tmp/acme/hecklr.yaml --suite smoke             # the candidate
# write .hecklr/mine/<day>/{clusters,proposals,conversations}.jsonl (ADR 0016a)
uv run hecklr ui -c /tmp/acme/hecklr.yaml --port 18481 --no-open &
HECKLR_UI_URL=http://127.0.0.1:18481 HECKLR_E2E_SHOTS=/tmp/shots npm run e2e
```

Voice: run `examples/acme-voice`'s `voice-timing` suite (rules brain, synthetic TTS), serve its
project, and set `HECKLR_E2E_VOICE_TRIAL=<run id>/<trial id>` to check the waveform, its turn
markers and playback.
