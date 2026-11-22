"""CI checks the injection eval's dataset format only (running it needs an API key)."""

import json
from pathlib import Path

from evals.injection.run import DIRECTIONS, load, validate_case

HERE = Path(__file__).resolve().parents[1] / "evals" / "injection"


def test_cases_are_well_formed_and_ids_unique():
    dev, test = load("dev"), load("test")
    ids = [c["id"] for c in dev + test]
    assert len(ids) == len(set(ids)), "duplicate ids (or a case in both sets)"
    assert 30 <= len(ids) <= 60


def test_every_set_covers_every_direction_and_label():
    for name in ("dev", "test"):
        rows = load(name)
        for direction in DIRECTIONS:
            labels = {c["label"] for c in rows if c["direction"] == direction}
            assert labels == {"flag", "allow"}, (name, direction, labels)


def test_validator_catches_bad_rows():
    assert validate_case({"id": "x", "direction": "sideways", "text": "t", "label": "maybe", "source": "x",
                          "note": "n", "extra": 1})
    line = (HERE / "dev.jsonl").read_text().splitlines()[0]
    assert validate_case(json.loads(line)) == []
