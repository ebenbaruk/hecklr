"""Errors of the fixer."""


class FixError(Exception):
    """The fixer cannot run: a bad target, editable list, agent command or failed run."""


class PatchRejectedError(FixError):
    """A proposed patch breaks a guardrail or does not apply; the candidate is dropped."""
