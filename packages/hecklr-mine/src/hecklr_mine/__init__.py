"""Hecklr miner: production logs to failure clusters and proposed regression scenarios.

Typical use (what ``hecklr mine`` does)::

    conversations = load_logs(Path("logs.jsonl"), LogFormat.JSONL)
    settings = MineSettings(world="acme", persona="busy-homeowner", model="openai/gpt-6-luna")
    result = await mine(conversations, settings, embedder=HashingEmbedder(), llm=llm,
                        existing=suite_scenarios)
    write_run(output_dir / MINE_DIR / mine_id, result, summarize(result, ...))
"""

from hecklr_mine.cluster import ClusterLabel, group_failures, keyword_label, llm_label
from hecklr_mine.detect import FailureReport, Signal, SignalKind, detect_failures
from hecklr_mine.embed import Embedder, HashingEmbedder, OpenAIEmbedder
from hecklr_mine.errors import IngestError, MineError, MissingExtraError
from hecklr_mine.ingest import CsvMapping, LogFormat, load_logs
from hecklr_mine.pipeline import BudgetExceededError, MineResult, MineSettings, mine
from hecklr_mine.presidio import PresidioDetector
from hecklr_mine.scrub import LlmPiiFinder, PiiDetector, Placeholders, RegexDetector, Scrubber
from hecklr_mine.store import (
    MINE_DIR,
    MineSummary,
    decide,
    load_queue,
    queued_scenarios,
    read_summary,
    summarize,
    write_run,
)
from hecklr_mine.synthesize import Duplicate
from hecklr_mine.synthetic import (
    PLANTED_MODES,
    Recovery,
    SyntheticLogSet,
    evaluate_recovery,
    generate_log_set,
)

__version__ = "0.1.0"

__all__ = [
    "MINE_DIR",
    "PLANTED_MODES",
    "BudgetExceededError",
    "ClusterLabel",
    "CsvMapping",
    "Duplicate",
    "Embedder",
    "FailureReport",
    "HashingEmbedder",
    "IngestError",
    "LlmPiiFinder",
    "LogFormat",
    "MineError",
    "MineResult",
    "MineSettings",
    "MineSummary",
    "MissingExtraError",
    "OpenAIEmbedder",
    "PiiDetector",
    "Placeholders",
    "PresidioDetector",
    "Recovery",
    "RegexDetector",
    "Scrubber",
    "Signal",
    "SignalKind",
    "SyntheticLogSet",
    "decide",
    "detect_failures",
    "evaluate_recovery",
    "generate_log_set",
    "group_failures",
    "keyword_label",
    "llm_label",
    "load_logs",
    "load_queue",
    "mine",
    "queued_scenarios",
    "read_summary",
    "summarize",
    "write_run",
]
