"""One-line descriptions of run failures, for ``hecklr run`` and the pytest plugin."""


def describe_error(exc: BaseException) -> str:
    """``Type: message``; an exception group (from the executor's task groups) lists its causes."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(sorted({describe_error(inner) for inner in exc.exceptions}))
    return f"{type(exc).__name__}: {exc}"
