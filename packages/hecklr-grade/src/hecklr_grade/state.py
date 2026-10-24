"""State assertions: a sandboxed boolean expression over the world snapshots and scenario facts.

Expressions run on simpleeval, never Python ``eval``, with three extra locks:

- World data is exposed through read-only views (``JsonObject``, tuples), so an expression can
  read records (``world.appointments['C-1042'].slot``) but can neither mutate them nor reach a
  Python method through them: attribute access on a view is a key lookup and nothing else.
- Attribute access on every other value is allow-listed: a few string methods, plus the date-time
  fields of ISO 8601 strings. Names starting with ``_`` are always refused.
- Only pure functions are available (no ``rand``: graders are deterministic).

Work is bounded too (issue #114), so no single operation runs long: a power whose result would
pass ``MAX_POWER_BITS`` is refused before it is computed, a value an expression builds may hold at
most ``MAX_VALUE_SIZE`` elements and characters (counting shared parts each time they appear),
and ``sum`` adds numbers only (its list form is quadratic). An expression still running after
``STATE_DEADLINE_S`` stops with ``StateExpressionTimeoutError``. ``StateGrader`` evaluates in a
worker thread, so a slow expression never stalls the other trials or Ctrl-C.

ISO 8601 date-time strings in the data (``2026-10-23T09:00``) become ``IsoDateTime`` values: still
strings (they compare and print as written) with ``year``, ``month``, ``day``, ``hour``,
``minute``, ``second``, ``weekday`` (Monday is 0, as ``datetime.weekday()``) and ``date``
attributes. The fields read the wall-clock time as written; an offset is not converted.
"""

import ast
import math
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime
from functools import partial
from typing import Any, Final

import anyio
import simpleeval  # type: ignore[import-untyped]  # simpleeval ships no type information

from hecklr_grade.base import criterion_as, errored, make_grade
from hecklr_schemas import Grade, GradeContext
from hecklr_schemas.scenario import StateCriterion

_ISO_DATE_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)


class StateExpressionError(ValueError):
    """The expression itself is invalid: syntax, a refused feature, an unknown name or result."""


class StateExpressionTimeoutError(StateExpressionError):
    """The expression was still running at its deadline."""


STATE_DEADLINE_S: Final = 1.0
"""Wall-clock time one expression may take. Real assertions take microseconds."""
MAX_POWER_BITS: Final = 4096
"""Largest integer power result, in bits. simpleeval's own limit (operands up to 4,000,000)
allows ``9**4000000``, which takes seconds to compute and cannot be interrupted (issue #114)."""
MAX_VALUE_SIZE: Final = 1_000_000
"""Largest value an expression may build, in elements plus string characters. A shared part counts
each time it appears: ``[[0] * 9999] * 9999`` is small in memory, but ``str``, ``sorted`` or
``==`` on it walk 10**8 elements in one uninterruptible call, holding the GIL (issue #114)."""


class StateLookupError(LookupError):
    """The data the expression reads is missing or of the wrong type, so the assertion fails."""


class IsoDateTime(str):
    """An ISO 8601 date-time string that also exposes its calendar fields."""

    __slots__ = ()

    @property
    def _parsed(self) -> datetime:
        return datetime.fromisoformat(self)

    @property
    def year(self) -> int:
        """Calendar year."""
        return self._parsed.year

    @property
    def month(self) -> int:
        """Month, 1 to 12."""
        return self._parsed.month

    @property
    def day(self) -> int:
        """Day of the month."""
        return self._parsed.day

    @property
    def hour(self) -> int:
        """Hour, 0 to 23, as written (no time-zone conversion)."""
        return self._parsed.hour

    @property
    def minute(self) -> int:
        """Minute, 0 to 59."""
        return self._parsed.minute

    @property
    def second(self) -> int:
        """Second, 0 to 59."""
        return self._parsed.second

    @property
    def weekday(self) -> int:
        """Day of the week: Monday is 0 and Sunday is 6."""
        return self._parsed.weekday()

    @property
    def date(self) -> str:
        """The ``YYYY-MM-DD`` part."""
        return self._parsed.date().isoformat()


def _as_date_time(text: str) -> str:
    if _ISO_DATE_TIME.fullmatch(text) is None:
        return text
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return text
    return IsoDateTime(text)


class JsonObject:
    """A read-only view of a JSON object: ``view.key`` and ``view['key']`` read a field.

    It deliberately has no public methods, so no field name can collide with one and no method
    can be reached from an expression. Iterating yields keys; ``keys()`` and ``values()`` are
    expression functions instead.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, object]) -> None:
        self._data = data

    def __getitem__(self, key: object) -> object:
        if not isinstance(key, str) or key not in self._data:
            raise StateLookupError(f"no key {key!r}")
        return wrap(self._data[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __eq__(self, other: object) -> bool:
        if isinstance(other, JsonObject):
            return self._data == other._data
        if isinstance(other, dict):
            return self._data == other
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]  # mutable-data view: unhashable, like dict

    def __repr__(self) -> str:
        return f"JsonObject({self._data!r})"


def wrap(value: object) -> object:
    """The expression-side view of a JSON value."""
    if isinstance(value, dict):
        return JsonObject(value)
    if isinstance(value, list):
        return tuple(wrap(item) for item in value)
    if isinstance(value, str):
        return _as_date_time(value)
    return value


_STRING_METHODS: Final = frozenset(
    {"lower", "upper", "strip", "casefold", "startswith", "endswith"}
)
_DATE_TIME_FIELDS: Final = frozenset(
    {"year", "month", "day", "hour", "minute", "second", "weekday", "date"}
)
_ALLOWED_ATTRIBUTES: Final[Mapping[type, frozenset[str]]] = {
    str: _STRING_METHODS,
    IsoDateTime: _STRING_METHODS,
}


def _keys(view: object) -> list[str]:
    if not isinstance(view, JsonObject):
        raise StateLookupError(f"keys() needs an object, got {type(view).__name__}")
    return list(view)


def _values(view: object) -> list[object]:
    if not isinstance(view, JsonObject):
        raise StateLookupError(f"values() needs an object, got {type(view).__name__}")
    return [view[key] for key in view]


def _sum(values: Iterable[Any], *start: object) -> object:
    if start:
        raise StateExpressionError("sum() takes one argument: the numbers to add")
    return sum(values)


FUNCTIONS: Final[Mapping[str, Callable[..., object]]] = {
    "len": len,
    "any": any,
    "all": all,
    "min": min,
    "max": max,
    "sum": _sum,
    "abs": abs,
    "round": round,
    "sorted": sorted,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "keys": _keys,
    "values": _values,
}
"""Functions an expression may call; all pure."""

NAMES: Final = ("world", "world_after", "world_before", "facts")
"""``world`` and ``world_after`` are the final state, ``facts`` the scenario's facts."""


def check_expression(expr: str) -> None:
    """Reject an expression that cannot be parsed, before any trial runs."""
    try:
        ast.parse(expr.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise StateExpressionError(f"cannot parse {expr!r}: {exc}") from exc


def evaluate(
    expr: str,
    *,
    world_after: Mapping[str, object],
    world_before: Mapping[str, object],
    facts: Mapping[str, object],
    deadline_s: float | None = None,
) -> bool:
    """Evaluate ``expr`` to a boolean, within ``deadline_s`` (``STATE_DEADLINE_S`` by default).

    Raises:
        StateExpressionError: the expression is invalid, refused, or not boolean;
            ``StateExpressionTimeoutError`` when it runs past its deadline.
        StateLookupError: it reads data that is missing or of the wrong type.
    """
    after = JsonObject(world_after)
    operators = dict(simpleeval.DEFAULT_OPERATORS)
    operators[ast.Pow] = _bounded_power
    evaluator: Any = simpleeval.EvalWithCompoundTypes(
        operators=operators,
        functions=dict(FUNCTIONS),
        names={
            "world": after,
            "world_after": after,
            "world_before": JsonObject(world_before),
            "facts": JsonObject(facts),
        },
    )
    evaluator.nodes[ast.Attribute] = _attribute_reader(evaluator)
    limit = STATE_DEADLINE_S if deadline_s is None else deadline_s
    evaluator._eval = _with_deadline(evaluator._eval, limit)
    for node_type in _BUILDERS:
        evaluator.nodes[node_type] = _size_checked(evaluator.nodes[node_type])
    check_expression(expr)
    try:
        result = evaluator.eval(expr.strip())
    except (StateLookupError, StateExpressionError):
        raise
    except (KeyError, IndexError, TypeError, ArithmeticError, ValueError) as exc:
        raise StateLookupError(f"{type(exc).__name__}: {exc}") from exc
    except (simpleeval.InvalidExpression, RecursionError) as exc:
        raise StateExpressionError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(result, bool):
        raise StateExpressionError(
            f"expression must be a boolean, got {type(result).__name__} {_brief(result)}"
        )
    return result


def _bounded_power(base: Any, exponent: Any) -> Any:
    """The ``**`` operator: ``simpleeval.safe_power`` with integer results bounded in size."""
    if (
        isinstance(base, int)
        and isinstance(exponent, int)
        and exponent > 0
        and abs(base) > 1
        and exponent * math.log2(abs(base)) > MAX_POWER_BITS
    ):
        # The operands are not quoted: a huge base has no printable decimal form (issue #38).
        raise simpleeval.NumberTooHigh(
            f"a power is too high: results are limited to {MAX_POWER_BITS} bits"
        )
    return simpleeval.safe_power(base, exponent)


_BUILDERS: Final = (
    ast.BinOp,
    ast.Call,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.ListComp,
    ast.GeneratorExp,
    ast.DictComp,
    ast.JoinedStr,
)
"""Nodes that can build a new value; reading a name, field or item only shares an existing one."""


def _with_deadline(
    step: Callable[[ast.AST], object], limit_s: float
) -> Callable[[ast.AST], object]:
    """``step`` (the evaluator's per-node recursion) checking the deadline before every node.

    With powers and values bounded, every operation an expression can reach is short, so checking
    between nodes stops a long expression within one operation of its deadline.
    """
    deadline = time.monotonic() + limit_s

    def bounded(node: ast.AST) -> object:
        if time.monotonic() > deadline:
            raise StateExpressionTimeoutError(f"the expression exceeded its {limit_s} s deadline")
        return step(node)

    return bounded


def _size_checked(handler: Callable[[ast.AST], object]) -> Callable[[ast.AST], object]:
    """A node handler that refuses a value past ``MAX_VALUE_SIZE`` as soon as it is built.

    It wraps the handler, not ``_eval``: simpleeval's own check of each result walks the value
    element by element, which on an oversized one is the very work being prevented.
    """

    def checked(node: ast.AST) -> object:
        value = handler(node)
        _check_size(value)
        return value

    return checked


def _check_size(value: object) -> None:
    """Raise ``StateExpressionError`` if ``value`` is larger than ``MAX_VALUE_SIZE``."""
    sizes: dict[int, int] = {}
    if _size(value, sizes) > MAX_VALUE_SIZE:
        raise StateExpressionError(
            f"a value is too large: values are limited to {MAX_VALUE_SIZE:,} elements"
        )


def _size(value: object, sizes: dict[int, int]) -> int:
    """Elements plus characters in ``value``, stopping once past ``MAX_VALUE_SIZE``.

    ``sizes`` remembers each container already measured, so a part shared a million times is
    walked once and counted a million times: measuring costs what the value holds in memory.
    """
    if isinstance(value, str):
        return max(1, len(value))
    if isinstance(value, JsonObject):
        value = value._data
    if not isinstance(value, Mapping | list | tuple | set | frozenset):
        return 1
    known = sizes.get(id(value))
    if known is not None:
        return known
    items: Iterable[object] = (
        (part for pair in value.items() for part in pair) if isinstance(value, Mapping) else value
    )
    total = 1
    for item in items:
        total += _size(item, sizes)
        if total > MAX_VALUE_SIZE:
            break
    sizes[id(value)] = total
    return total


_BRIEF_CHARS: Final = 60
"""How much of a non-boolean result an error message quotes."""


def _brief(value: object) -> str:
    """A short repr for an error message.

    ``repr`` itself raises ``ValueError`` for an int over Python's int-to-str digit limit, which
    an expression like ``12**121212`` produces (issue #38).
    """
    try:
        text = repr(value)
    except ValueError:
        return "too large to print"
    return text if len(text) <= _BRIEF_CHARS else f"{text[:_BRIEF_CHARS]}..."


def _attribute_reader(evaluator: Any) -> Callable[[ast.Attribute], object]:
    """A replacement for simpleeval's attribute handler: allow-list, plus key lookup on views.

    simpleeval's own handler tries ``getattr`` before key lookup, so a collection named ``items``
    would resolve to ``dict.items``; replacing the handler in ``evaluator.nodes`` (its extension
    point, as in simpleeval's README) makes views pure data.
    """

    def read(node: ast.Attribute) -> object:
        if node.attr.startswith("_"):
            raise simpleeval.FeatureNotAvailable(f"access to {node.attr!r} is not allowed")
        owner = evaluator._eval(node.value)  # the evaluator's own recursion, as its handlers do
        if isinstance(owner, JsonObject):
            return owner[node.attr]
        if node.attr in _DATE_TIME_FIELDS and isinstance(owner, str):
            if not isinstance(owner, IsoDateTime):
                # The data is wrong, not the expression: e.g. an agent that wrote "10/27/2026"
                # (fault F2) must fail the assertion, not error out of it.
                raise StateLookupError(f"{owner!r} is not an ISO 8601 date-time")
            return getattr(owner, node.attr)
        if node.attr not in _ALLOWED_ATTRIBUTES.get(type(owner), frozenset()):
            raise simpleeval.FeatureNotAvailable(
                f"attribute {node.attr!r} is not available on {type(owner).__name__}"
            )
        return getattr(owner, node.attr)

    return read


class StateGrader:
    """``state``: the expression holds on the world after the trial (see the module docstring)."""

    name = "state"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Pass when the expression is true; missing data fails, an invalid expression errors."""
        criterion = criterion_as(ctx, StateCriterion)
        label = criterion.description or criterion.expr
        check = partial(
            evaluate,
            criterion.expr,
            world_after=ctx.trial.world_after.collections,
            world_before=ctx.trial.world_before.collections,
            facts=ctx.scenario.facts,
        )
        try:
            # Off the event loop, so other trials' timeouts and Ctrl-C keep working (#114). A
            # cancelled grade abandons the thread, which stops by itself at the deadline.
            holds = await anyio.to_thread.run_sync(check, abandon_on_cancel=True)
        except StateExpressionError as exc:
            return errored(ctx, self, f"invalid state expression {criterion.expr!r}: {exc}")
        except StateLookupError as exc:
            return make_grade(ctx, self, passed=False, rationale=f"{label}: data missing ({exc})")
        return make_grade(ctx, self, passed=holds, rationale=f"{label}: {holds}")
