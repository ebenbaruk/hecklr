from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_yaml() -> Callable[[str], dict[str, Any]]:
    def load(name: str) -> dict[str, Any]:
        data = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    return load
