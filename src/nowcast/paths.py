from pathlib import Path

from nowcast.config import REPO_ROOT, env

ZONES = ("ref", "bronze", "silver", "gold", "models", "state", "tmp")


def data_root() -> Path:
    value = env("DATA_ROOT")
    if value is None:
        return REPO_ROOT / "data"
    path = Path(value)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def zone(name: str) -> Path:
    if name not in ZONES:
        raise ValueError(f"Unknown data zone: {name}")
    return data_root() / name


def ensure_layout() -> Path:
    root = data_root()
    for name in ZONES:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "state" / "health").mkdir(exist_ok=True)
    return root
