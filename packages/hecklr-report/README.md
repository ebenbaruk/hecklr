# hecklr-report

Hecklr reports: terminal, Markdown, HTML, JSON and JUnit.

```python
from hecklr_report import analyze, gate_exit_code, render_html, render_markdown, render_terminal

analysis = analyze(candidate, baseline, config.stats, scenarios, personas=personas)
render_terminal(analysis, console)  # Rich tables, every number with its CI
comment = render_markdown(analysis, report_url=url)  # PR comment, under 30 lines
render_html(analysis, candidate, run_dir / "report.html")  # one self-contained file
exit_code = gate_exit_code(analysis, config.gate)  # 0 pass, 1 regression, 2 inconclusive, 3 error
```

`render_json` writes the full `Analysis` (with `schema_version`); `render_junit` writes one test
case per scenario. Decisions: `docs/adr/0007b-report-analysis-and-gate.md`.

Part of [Hecklr](../../README.md). Owned by WS7; see `docs/SPEC.md`.
