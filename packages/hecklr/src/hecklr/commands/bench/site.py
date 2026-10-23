"""The static leaderboard: the site's source files plus the results, written to one directory.

The rows are written as ``data.js`` (``window.HECKLR_BENCH = {...}``) and loaded with a script
tag, so ``dist/index.html`` opens straight from disk: browsers refuse ``fetch`` on ``file://``.
Nothing is deployed; the directory can be served by any static file server.
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from hecklr.commands.bench.result import StackResult
from hecklr.commands.bench.spec import Manifest
from hecklr_core import ConfigError, ConfigIssue

DATA_FILE: Final = "data.js"


def load_results(results_dir: Path) -> list[StackResult]:
    """Every ``*.json`` row under ``results_dir``, sorted by stack id.

    Raises:
        ConfigError: a file that is not a valid row.
    """
    rows = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            rows.append(StackResult.model_validate_json(path.read_bytes()))
        except ValidationError as exc:
            raise ConfigError([ConfigIssue(path, None, f"not a benchmark result: {exc}")]) from exc
    return rows


def build_site(manifest: Manifest, rows: list[StackResult], *, source: Path, out: Path) -> Path:
    """Write the leaderboard into ``out`` (replacing it); returns ``out/index.html``.

    Rows of another benchmark version are left out: their suite differs.
    """
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(source, out)
    current = [row for row in rows if row.benchmark_version == manifest.version]
    data = {
        "benchmark_version": manifest.version,
        "seed": manifest.seed,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tracks": {name: track.model_dump(mode="json") for name, track in manifest.tracks.items()},
        "models": manifest.models.model_dump(mode="json"),
        "rows": [row.model_dump(mode="json") for row in current],
    }
    # ensure_ascii keeps U+2028/2029, which end a JavaScript line, out of the script.
    payload = json.dumps(data, ensure_ascii=True, indent=1)
    (out / DATA_FILE).write_text(f"window.HECKLR_BENCH = {payload};\n", encoding="utf-8")
    return out / "index.html"
