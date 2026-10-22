"""Hecklr miner: log ingestion, PII scrubbing, failure clustering, scenario synthesis."""

from hecklr_mine.errors import IngestError, MineError, MissingExtraError
from hecklr_mine.ingest import CsvMapping, LogFormat, load_logs

__version__ = "0.1.0"

__all__ = [
    "CsvMapping",
    "IngestError",
    "LogFormat",
    "MineError",
    "MissingExtraError",
    "load_logs",
]
