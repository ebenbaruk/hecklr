# hecklr (meta-package + CLI) progress

Owner: WS7 (CLI), WS0 (scaffold). Spec: `docs/SPEC.md` → "Developer experience".

## Done

- Meta-package with extras `[voice]`, `[local-voice]`, `[mine]`, `[ui]`, `[all]`.
- Typer CLI skeleton: `hecklr version` works; every spec command (`init`, `run`, `compare`, `gate`,
  `report`, `replay`, `mine`, `fix`, `calibrate`, `bench`, `ui`, `doctor`) is registered and exits
  with code 69 (`EX_UNAVAILABLE`) and a clear "not implemented yet" message.

## Next

- WS7 replaces each stub with the real command and the `gate` exit codes (0/1/2/3).

## Open questions

- None yet.
