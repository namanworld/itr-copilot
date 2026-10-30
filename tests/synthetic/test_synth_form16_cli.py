"""itr synth-form16 writes a PDF, a truth JSON and the user's facts per case."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from itr.domain.form16 import Form16
from itr.domain.form16_mapping import UserExtras
from itr.domain.rules import TaxRules
from itr.interfaces.cli.main import main
from itr.synthetic.form16_generator import generate

ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    text = (ROOT / "rules/fy2025_26.json").read_text(encoding="utf-8")
    return TaxRules.model_validate(json.loads(text, parse_float=Decimal))


def test_writes_three_files_per_case(
    tmp_path: Path, rules: TaxRules, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["synth-form16", "--n", "3", "--seed", "42", "--out", str(tmp_path)])

    assert code == 0
    assert "Wrote 3 synthetic Form 16s (seeds 42 to 44)" in capsys.readouterr().out
    for number, seed in [(1, 42), (2, 43), (3, 44)]:
        name = f"F{number:03d}"
        form16, extras = generate(seed, rules)
        assert (tmp_path / f"{name}.pdf").read_bytes().startswith(b"%PDF")
        truth = (tmp_path / f"{name}.truth.json").read_text(encoding="utf-8")
        user = (tmp_path / f"{name}.user.json").read_text(encoding="utf-8")
        assert Form16.model_validate_json(truth) == form16
        assert UserExtras.model_validate_json(user) == extras


def test_manifest_records_each_seed(tmp_path: Path) -> None:
    main(["synth-form16", "--n", "2", "--seed", "7", "--out", str(tmp_path)])

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "fy": "2025-26",
        "cases": [{"file": "F001", "seed": 7}, {"file": "F002", "seed": 8}],
    }


def test_same_seed_writes_identical_files(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    main(["synth-form16", "--n", "2", "--seed", "5", "--out", str(first)])
    main(["synth-form16", "--n", "2", "--seed", "5", "--out", str(second)])

    for path in sorted(first.iterdir()):
        assert path.read_bytes() == (second / path.name).read_bytes(), path.name


def test_n_must_be_at_least_one(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["synth-form16", "--n", "0", "--out", str(tmp_path)])
    assert exit_info.value.code == 2
