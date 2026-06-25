"""
Config loader — reads .env, validates required keys, exposes typed settings.
All secrets come from environment only; never hardcoded.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Walk up from this file to find the .env (works wherever cwd is)
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required env var '{key}' is missing or empty. "
            f"Copy .env.example → .env and fill in the value."
        )
    return val


def _optional(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)


class Settings:
    """Centralised, lazily-evaluated settings object."""

    @property
    def gemini_api_key(self) -> str:
        return _require("GEMINI_API_KEY")

    @property
    def dashscope_api_key(self) -> str | None:
        # Optional — Qwen is the fallback model; primary pipeline uses Gemini.
        # Set DASHSCOPE_API_KEY in .env before using QwenExtractor.
        return _optional("DASHSCOPE_API_KEY")

    @property
    def rclone_remote(self) -> str | None:
        return _optional("RCLONE_REMOTE") or None

    @property
    def rclone_path(self) -> str | None:
        return _optional("RCLONE_PATH") or None

    @property
    def connector(self) -> str:
        return _optional("CONNECTOR", "local") or "local"

    @property
    def github_token(self) -> str | None:
        return _optional("GITHUB_TOKEN") or None

    @property
    def openrouter_api_key(self) -> str | None:
        return _optional("OPENROUTER_API_KEY") or None

    # Model routing
    gemini_model: str = "gemini-2.5-flash"
    qwen_model: str = "qwen-vl-plus"
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # Paths
    corpus_dir: Path = Path(__file__).parent / "test_corpus" / "files"
    ground_truth_dir: Path = Path(__file__).parent / "ground_truth"
    output_dir: Path = Path(__file__).parent / "output_data"

    # Pipeline tuning
    confidence_threshold: float = 0.70
    max_retries: int = 3
    rate_limit_rps: float = 0.5  # 1500 req/day free tier → ~1/60s; use 0.5 for headroom


settings = Settings()
