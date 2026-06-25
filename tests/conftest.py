"""
Shared pytest fixtures for the resume-parser test suite.
"""
from __future__ import annotations

import sys
import pytest
from pathlib import Path

# Ensure repo root is on sys.path for all tests
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--skip-regression",
        action="store_true",
        default=False,
        help="Skip regression tests (avoids LLM API calls; useful for fast local runs)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "regression: mark test as a golden-set regression test (makes real LLM calls)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--skip-regression"):
        skip_mark = pytest.mark.skip(reason="--skip-regression passed")
        for item in items:
            if "regression" in item.keywords:
                item.add_marker(skip_mark)
