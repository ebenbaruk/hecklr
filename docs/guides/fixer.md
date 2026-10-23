# Fixer

!!! warning "In development"
    `hecklr fix` is being built (workstream WS15). This page documents the command as specified;
    until it lands, `hecklr fix` exits with "not implemented yet". Flags and output may change
    before the first release.

Given a failure cluster, the fixer proposes changes to the files you allow it to edit (your system
prompt, your tool descriptions), validates each candidate against your suite with the same
statistics as the gate, and hands you the winning diff, or tells you plainly that nothing beat the
baseline.

```bash
hecklr fix <cluster id>          # propose, validate, print the winning diff and its comparison
hecklr fix <cluster id> --pr     # also open a pull request with gh
```

## How a fix is chosen

1. **Input:** one failure cluster (from `hecklr mine`, or the failed trials of a run) plus the files
   listed under `agent.editable` in `hecklr.yaml`:

    ```yaml
    agent:
      editable: [prompts/system.md, tools/descriptions.yaml]
    ```

2. **Generate** N candidate patches (default 5) with an LLM that sees the failing transcripts, the
   judges' rationales and the current files.
3. **Targeted pass:** run each candidate on the cluster's scenarios; drop every candidate without a
   significant improvement.
4. **Guard pass:** run the survivors on the full suite; drop any with a significant regression on
   any tag.
5. **Output** the winning diff with its comparison report and, with `--pr`, a pull request.

## Guardrails

- The fixer can never edit scenarios, graders, rubrics, or any file outside `editable`.
- It reports "no candidate beat the baseline" honestly; it never lowers a bar to produce a win.
- A human always merges.
