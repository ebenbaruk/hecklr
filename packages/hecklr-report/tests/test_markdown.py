"""The PR comment: content, escaping, and the 30-line limit of US-05."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hecklr_report import Analysis, FlipGroup, JudgeTrust, analyze, render_markdown
from hecklr_report.markdown import escape
from hecklr_schemas import Scenario, Verdict
from hecklr_stats import wilson_estimate

if TYPE_CHECKING:
    from conftest import Kit
    from hecklr_core import RunResults

URL = "https://ci.example/run/42/report.html"


def crowded(
    analysis: Analysis,
    *,
    causes: int,
    per_cause: int,
    fixed: int,
    slices: int,
    judges: int,
    problems: int,
    rationale: str,
) -> Analysis:
    """``analysis`` with as many flips, slice verdicts, untrusted judges and problems as asked."""
    suite = analysis.comparisons[0]
    flips = [
        FlipGroup(
            direction="pass_to_fail",
            cause=f"judge:rubric-{i} `x` | y",
            scenario_ids=[f"c{i}-s{j}" for j in range(per_cause)],
            rationale=rationale,
        )
        for i in range(causes)
    ] + [
        FlipGroup(direction="fail_to_pass", cause=f"state: fixed {i}", scenario_ids=[f"f{i}"])
        for i in range(fixed)
    ]
    slice_verdicts = [
        suite.model_copy(
            update={
                "slice": {"tag": f"tag-{i}"},
                "verdict": Verdict.REGRESSION if i % 2 else Verdict.IMPROVEMENT,
            }
        )
        for i in range(slices)
    ]
    trust = [
        JudgeTrust(
            grader=f"judge:r{i}",
            grader_version="v",
            judge_model=None,
            n=50,
            kappa=wilson_estimate(30, 50),
            trusted=False,
        )
        for i in range(judges)
    ]
    inconclusive = suite.model_copy(update={"verdict": Verdict.INCONCLUSIVE, "trials_needed": 7})
    return analysis.model_copy(
        update={
            "flips": flips,
            "comparisons": [inconclusive, *slice_verdicts, *analysis.comparisons[1:]],
            "judges": trust,
            "problems": [f"problem {i} <b>" for i in range(problems)],
            "verdict": Verdict.INCONCLUSIVE,
        }
    )


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    causes=st.integers(0, 60),
    per_cause=st.integers(1, 40),
    fixed=st.integers(0, 20),
    slices=st.integers(0, 30),
    judges=st.integers(0, 10),
    problems=st.integers(0, 6),
    rationale=st.text(min_size=0, max_size=400),
)
def test_us05_comment_stays_under_30_lines(
    regression: tuple[Analysis, RunResults],
    causes: int,
    per_cause: int,
    fixed: int,
    slices: int,
    judges: int,
    problems: int,
    rationale: str,
) -> None:
    analysis = crowded(
        regression[0],
        causes=causes,
        per_cause=per_cause,
        fixed=fixed,
        slices=slices,
        judges=judges,
        problems=problems,
        rationale=rationale,
    )

    comment = render_markdown(analysis, report_url=URL)

    assert len(comment.splitlines()) < 30


def test_us05_worst_case_comment_summarizes_what_does_not_fit(
    regression: tuple[Analysis, RunResults],
) -> None:
    analysis = crowded(
        regression[0],
        causes=50,
        per_cause=20,
        fixed=9,
        slices=30,
        judges=8,
        problems=5,
        rationale="Agent said <script>alert(1)</script> and [click](http://evil.example)\nnewline",
    )

    comment = render_markdown(analysis, report_url=URL)
    lines = comment.splitlines()

    assert len(lines) < 30
    assert "**Flipped pass → fail: 1000 scenarios**" in comment
    assert "+45 more causes (900 scenarios)" in comment
    assert "+16 more" in comment
    assert "+7 more causes (7 scenarios)" in comment
    assert "+12 more" in comment
    assert "about 7 more trials per scenario" in comment
    assert "<script>" not in comment
    assert "](http://evil" not in comment
    assert "<b>" not in comment
    assert lines[-1] == f"[Full report]({URL})"


def test_us05_regression_comment_shows_verdict_delta_flips_and_latency(
    regression: tuple[Analysis, RunResults],
) -> None:
    analysis = regression[0]
    suite = analysis.comparisons[0]

    comment = render_markdown(analysis, report_url=URL)

    assert comment.splitlines()[0] == (
        "### Hecklr · REGRESSION · suite `ci` · agent `acme-support`"
    )
    assert f"\N{MINUS SIGN}{abs(100 * suite.delta.value):.1f} pts" in comment
    assert "(95% CI)" in comment
    assert "**Flipped pass → fail:" in comment
    assert "`judge:resolved`" in comment
    assert "The agent never confirmed the new slot." in comment
    assert "**Latency p95** suite:" in comment
    assert " ms [" in comment.split("**Latency p95** suite:")[1].split(" → ")[0]
    assert "**Run** 200 of 200 trials analyzed" in comment
    assert len(comment.splitlines()) < 30


def test_single_run_comment_lists_failing_scenarios_by_cause(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = kit.RunBuilder()
    builder.suite({"s00": 1.0, "s01": 0.0, "s02": 0.0}, 3, rng_seed=1)
    builder.trial("s03", status="harness_error")

    comment = render_markdown(analyze(builder.build(), None, kit.stats, scenarios))

    assert comment.startswith("### Hecklr · RUN SUMMARY")
    assert "**Pass rate** 33.3% [" in comment
    assert "pass^3" in comment
    assert "**Scenarios with failures: 2**" in comment
    assert "- `judge:resolved` (2): `s01`, `s02`" in comment
    assert "1 harness errors" in comment
    assert "Full report" not in comment


def test_comment_says_when_it_cannot_decide(kit: type[Kit], scenarios: dict[str, Scenario]) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00", status="harness_error")

    comment = render_markdown(analyze(builder.build(), None, kit.stats, scenarios))

    assert "CANNOT DECIDE" in comment
    assert "**Cannot gate:** no trial of the candidate run could be analyzed" in comment


def test_inconclusive_without_a_trial_estimate_asks_for_scenarios(
    regression: tuple[Analysis, RunResults],
) -> None:
    analysis = regression[0]
    suite = analysis.comparisons[0].model_copy(
        update={"verdict": Verdict.INCONCLUSIVE, "trials_needed": None}
    )
    comment = render_markdown(
        analysis.model_copy(update={"comparisons": [suite], "verdict": Verdict.INCONCLUSIVE})
    )

    assert "more trials are unlikely to decide it; add scenarios" in comment


def test_escape_neutralizes_markdown_and_html() -> None:
    assert escape("[x](http://e) <img> *b* `c`\n_d_") == (
        r"\[x\]\(http://e\) \<img\> \*b\* \`c\` \_d\_"
    )


def test_us05_every_slice_delta_carries_its_interval(
    regression: tuple[Analysis, RunResults],
) -> None:
    analysis = crowded(
        regression[0], causes=0, per_cause=1, fixed=0, slices=4, judges=0, problems=0, rationale=""
    )

    comment = render_markdown(analysis)
    slice_lines = [line for line in comment.splitlines() if line.startswith("**Slices")]

    assert len(slice_lines) == 2
    for line in slice_lines:
        for item in line.split("): ", 1)[1].split("`tag-")[1:]:
            assert re.search(r"pts \[[^\]]+, [^\]]+\]", item), item


def test_us05_headline_values_carry_their_intervals(
    regression: tuple[Analysis, RunResults],
) -> None:
    headline = next(
        line
        for line in render_markdown(regression[0]).splitlines()
        if line.startswith("**Pass rate**")
    )

    before, after = headline.split(" → ")
    assert re.search(r"% \[[^\]]+\]$", before)
    assert re.match(r"[\d.]+% \[[^\]]+\]:", after)


def test_escape_breaks_github_mentions() -> None:
    escaped = escape("ping @acme/oncall and @bob")

    assert "@acme" not in escaped
    assert "@bob" not in escaped
    assert escaped.replace("\N{ZERO WIDTH JOINER}", "") == "ping @acme/oncall and @bob"


def test_issue104_untrusted_judges_kappa_carries_its_interval(
    regression: tuple[Analysis, RunResults],
) -> None:
    analysis = crowded(
        regression[0],
        causes=0,
        per_cause=1,
        fixed=0,
        slices=0,
        judges=1,
        problems=0,
        rationale="",
    )

    line = next(
        x for x in render_markdown(analysis).splitlines() if x.startswith("**Untrusted judges**")
    )

    assert "`judge:r0` κ=0.60 [0.46, 0.72]" in line


def test_issue101_comment_names_conversations_the_guard_gave_up_on(
    regression: tuple[Analysis, RunResults],
) -> None:
    analysis = regression[0]
    counts = analysis.counts.model_copy(
        update={"guard_abandoned": 3, "contradiction_rate": wilson_estimate(3, 200)}
    )

    comment = render_markdown(analysis.model_copy(update={"counts": counts}))

    rate = "simulator contradictions 1.5% [0.5%, 4.3%]"
    assert f"{rate}, 3 conversations abandoned by the consistency guard" in comment
