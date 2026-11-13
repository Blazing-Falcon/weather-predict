import json
from datetime import datetime


def model_card(root, version):
    return json.loads((root / "models" / version / "card.json").read_text(encoding="utf-8"))


def cards(root):
    result = []
    for path in (root / "models").glob("*/card.json"):
        try:
            card = model_card(root, path.parent.name)
        except FileNotFoundError:
            continue
        result.append(card)
    return sorted(
        result,
        key=lambda card: (datetime.fromisoformat(card["created_at"]), card["version"]),
        reverse=True,
    )


def has_card(root, version):
    return (root / "models" / version / "card.json").is_file()
