# Examples

- `acme-maintenance/`: reference agent: chat (WS8), voice (WS11) and email (WS12). See its
  [README](acme-maintenance/README.md).
- `acme-faults/`: the 12 seeded fault variants F1-F12 as one-line overlays of the reference
  agent's editable files; F1-F6 (chat) are in, F7-F12 come with WS11 and WS12. See its
  [README](acme-faults/README.md).
- `loopback-voice/`: voice calibration agent (SC4). WS11.

## acme-email (WS12b)

`acme-email/` is the reference agent by email: the acme-maintenance brains behind an email front
end (webhook, SMTP/IMAP mailbox, in process), with its own `hecklr.yaml` (`hecklr.mailbox.yaml`
for SMTP/IMAP), email personas, 14 scripted and 13 guided email scenarios. Its `mailer.yaml` is
what faults F11 (replies lose the thread) and F12 (a hung tool silences the agent) change in
`acme-faults/F11` and `acme-faults/F12`. See its [README](acme-email/README.md) and
[RESULTS](acme-email/RESULTS.md).
