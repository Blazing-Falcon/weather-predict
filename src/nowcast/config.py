import os
from functools import cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

load_dotenv(REPO_ROOT / ".env", override=False)


@cache
def settings() -> dict:
    return yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8"))


@cache
def retention() -> dict:
    return yaml.safe_load((CONFIG_DIR / "retention.yaml").read_text(encoding="utf-8"))


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)
