"""Number formatting shared by the text renderers: every value prints with its interval."""

from typing import Literal

from hecklr_schemas import Comparison, Estimate, Slice

Unit = Literal["percent", "ms", "usd", "number"]


def unit_of(metric: str) -> Unit:
    """How values of ``metric`` print: rates as percents, latencies in ms, costs in dollars."""
    if metric.startswith(("pass_rate", "pass^", "pass@", "backchannel_stop_rate")):
        return "percent"
    if metric.startswith(("response_latency", "barge_in_reaction")):
        return "ms"
    if metric.startswith("cost_usd"):
        return "usd"
    return "number"


def value(x: float, unit: Unit) -> str:
    """One number in ``unit``."""
    match unit:
        case "percent":
            return f"{100 * x:.1f}%"
        case "ms":
            return f"{x:,.0f} ms"
        case "usd":
            return f"${x:,.4f}"
        case "number":
            return f"{x:,.2f}"


def interval(e: Estimate, unit: Unit) -> str:
    """The interval alone, e.g. ``[75.1%, 88.0%]``."""
    return f"[{value(e.ci_low, unit)}, {value(e.ci_high, unit)}]"


def estimate(e: Estimate, unit: Unit) -> str:
    """A value with its interval, e.g. ``82.0% [75.1%, 88.0%]``."""
    return f"{value(e.value, unit)} {interval(e, unit)}"


def confidence(e: Estimate) -> str:
    """The interval's coverage, e.g. ``95% CI``."""
    return f"{100 * e.confidence:g}% CI"


def delta(c: Comparison) -> str:
    """A comparison's change with its interval, e.g. ``-7.0 pts [-11.0, -3.0]`` or ``+308 ms``.

    Rates change in percentage points; negative numbers print with a true minus sign.
    """
    d = c.delta
    if unit_of(c.metric) == "percent":
        low, high = _signed(100 * d.ci_low, 1), _signed(100 * d.ci_high, 1)
        return f"{_signed(100 * d.value, 1)} pts [{low}, {high}]"
    return f"{_signed(d.value, 0)} ms [{_signed(d.ci_low, 0)}, {_signed(d.ci_high, 0)}]"


def p_value(p: float) -> str:
    """A p- or q-value, never printed as a bare 0."""
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def p_equals(p: float) -> str:
    """``p=0.012`` or ``p<0.001``."""
    text = p_value(p)
    return f"p{text}" if text.startswith("<") else f"p={text}"


def look(c: Comparison) -> str:
    """The look and alpha spent of a group-sequential comparison; empty for a fixed design."""
    if c.alpha_spent is None:
        return ""
    return f" · adaptive look {c.look}, \N{GREEK SMALL LETTER ALPHA} spent {c.alpha_spent:.3f}"


def abandoned(conversations: int) -> str:
    """How many conversations the simulator's consistency guard gave up on (issue #101)."""
    plural = "s" if conversations != 1 else ""
    return f"{conversations} conversation{plural} abandoned by the consistency guard"


def rescore_advice(earlier_judge_model: str) -> str:
    """What to do after a judge model change: the spec's re-score of the labeled set (#102)."""
    return (
        "judge verdicts may have moved with the judge, not the agent; re-score the labeled set: "
        f"hecklr calibrate --judges --set DIR --drift-from {earlier_judge_model}"
    )


def slice_label(slice_: Slice) -> str:
    """``suite`` for the whole run, else e.g. ``channel=voice``."""
    return ", ".join(f"{k}={v}" for k, v in sorted(slice_.items())) or "suite"


def _signed(x: float, digits: int) -> str:
    # A true minus sign reads better than a hyphen next to brackets, and +0.0 never prints as -0.0.
    rounded = round(x, digits)
    text = f"{abs(rounded):,.{digits}f}"
    if rounded > 0:
        return f"+{text}"
    if rounded < 0:
        return f"\N{MINUS SIGN}{text}"
    return text
