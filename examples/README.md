# Examples

- `acme-maintenance/`: reference agent: chat (WS8), voice (WS11) and email (WS12). See its
  [README](acme-maintenance/README.md).
- `acme-faults/`: the 12 seeded fault variants F1-F12 as one-line overlays of the reference
  agent's editable files: F1-F6 chat, F7-F10 voice, F11-F12 email. See its
  [README](acme-faults/README.md).

## Voice (WS11)

- `acme-voice/`: the acme-maintenance agent over voice: VAD, faster-whisper, the acme brain
  (imported from `acme-maintenance`), Kokoro or synthetic speech, served over the `pcm`
  WebSocket voice protocol. Its `voice.yaml` holds the knobs the voice faults flip; F7-F10 live in
  `acme-faults/` next to the chat faults. Suites for turn-taking and the SNR degradation curve,
  and `scripts/voice-fault-matrix.sh`. See its [README](acme-voice/README.md) and
  [RESULTS](acme-voice/RESULTS.md).
- `voice-loopback/`: the networked calibration agent (SC4) and the load run (SC3): replies a
  known delay after the caller stops and stops a known delay after a barge-in, in its own
  process over a real socket. See its [README](voice-loopback/README.md).

## acme-email (WS12b)

`acme-email/` is the reference agent by email: the acme-maintenance brains behind an email front
end (webhook, SMTP/IMAP mailbox, in process), with its own `hecklr.yaml` (`hecklr.mailbox.yaml`
for SMTP/IMAP), email personas, 14 scripted and 13 guided email scenarios. Its `mailer.yaml` is
what faults F11 (replies lose the thread) and F12 (a hung tool silences the agent) change in
`acme-faults/F11` and `acme-faults/F12`. See its [README](acme-email/README.md) and
[RESULTS](acme-email/RESULTS.md).
