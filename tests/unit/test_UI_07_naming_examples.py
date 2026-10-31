"""FX-UI-07: shared Python/Web naming examples derived from PDD 8.2."""

import json
from pathlib import Path

from holm_domain.instance_names import default_instance_name


def test_UI_07_shared_naming_examples() -> None:
    samples = json.loads(
        (Path(__file__).parents[1] / "fixtures/instance_names/UI-07.json").read_text()
    )
    for sample in samples:
        for sequence in range(1, len(sample["occupied"]) + 2):
            candidate = default_instance_name(sample["model_name"], sequence)
            if candidate not in sample["occupied"]:
                break
        assert candidate == sample["expected"]
        assert 2 <= len(candidate) <= 64
