"""Errors the miner raises; each one names what the user has to fix."""


class MineError(Exception):
    """Base class for miner errors."""


class IngestError(MineError):
    """A log export could not be read or converted; the message names the file and record."""


class MissingExtraError(MineError):
    """An optional dependency is not installed; the message names the extra to install."""
