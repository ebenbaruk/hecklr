"""Custom graders: third-party ``Grader``s registered under the ``hecklr.graders`` entry points.

A package registers a zero-argument callable that returns a ``Grader`` (a ``Grader`` class
works)::

    [project.entry-points."hecklr.graders"]
    refund_amount = "acme_graders:RefundAmountGrader"

and a scenario uses it with ``{type: custom, name: refund_amount, params: {...}}``; the grader
reads ``params`` from ``ctx.criterion``.
"""

from importlib.metadata import entry_points

from hecklr_schemas import GRADER_ENTRY_POINT_GROUP, Grader


class CustomGraderError(LookupError):
    """A custom grader is missing, ambiguous, or does not implement ``Grader``."""


def load_custom_grader(name: str) -> Grader:
    """Instantiate the custom grader registered as ``name``."""
    found = entry_points(group=GRADER_ENTRY_POINT_GROUP, name=name)
    if not found:
        raise CustomGraderError(
            f"no grader {name!r} in entry-point group {GRADER_ENTRY_POINT_GROUP!r}; "
            "is the package that provides it installed?"
        )
    if len(found) > 1:
        sources = ", ".join(sorted(entry.value for entry in found))
        raise CustomGraderError(f"grader {name!r} is registered more than once: {sources}")
    (entry,) = found
    grader = entry.load()()
    if not isinstance(grader, Grader):
        raise CustomGraderError(f"{entry.value} did not produce a Grader (got {type(grader)!r})")
    return grader
