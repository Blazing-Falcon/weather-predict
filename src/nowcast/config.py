import os
from functools import cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

if (REPO_ROOT / ".env").is_file():
    load_dotenv(REPO_ROOT / ".env", override=False)

CONFIG_DIR = Path(os.environ.get("NOWCAST_CONFIG_DIR", REPO_ROOT / "config"))
WEB_DIR = Path(os.environ.get("NOWCAST_WEB_DIR", REPO_ROOT / "web"))


@cache
def settings() -> dict:
    return yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8"))


@cache
def retention() -> dict:
    return yaml.safe_load((CONFIG_DIR / "retention.yaml").read_text(encoding="utf-8"))


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)
