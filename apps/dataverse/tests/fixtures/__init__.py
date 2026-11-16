"""Dataverse native API responses, hand-built from the 6.x guides (the dev Dataverse is down; not recorded live)."""

import copy
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent


def load(name: str) -> dict:
    """The whole response body of `<name>.json` (a fresh copy)."""
    with open(_DIR / f"{name}.json", encoding="utf-8") as handle:
        return copy.deepcopy(json.load(handle))


def data(name: str) -> dict:
    return load(name)["data"]
