import csv
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from itr.domain.rules import TaxRules
from itr.infrastructure.rules import json_loader
from itr.infrastructure.rules.json_loader import load_rules


def _read_rules_data() -> Any:
    rules_path = Path(__file__).parents[2] / "rules/fy2025_26.json"
    return json.loads(rules_path.read_text(encoding="utf-8"))


def test_load_rules_accepts_surcharge_bands_starting_at_threshold() -> None:
    rules = load_rules("2025-26")

    assert rules.new_regime.surcharge_bands[0].min_income == Decimal("5000000")


def test_surcharge_rules_are_specific_to_each_regime() -> None:
    rules = load_rules("2025-26")

    assert rules.new_regime.surcharge_bands[-1].rate == Decimal("0.25")
    assert rules.old_regime.surcharge_bands[-1].rate == Decimal("0.37")


def test_87a_marginal_relief_is_configured_per_regime() -> None:
    rules = load_rules("2025-26")

    assert rules.new_regime.rebate_87a.marginal_relief is True
    assert rules.old_regime.rebate_87a.marginal_relief is False


def test_80d_rules_express_separate_limits_and_included_checkups() -> None:
    rules = load_rules("2025-26")
    section_80d = next(d for d in rules.deductions if d.code == "80D")

    assert section_80d.health_insurance is not None
    assert section_80d.health_insurance.self_and_family.regular_limit == Decimal(
        "25000"
    )
    assert section_80d.health_insurance.parents.senior_citizen_limit == Decimal("50000")
    assert section_80d.health_insurance.preventive_checkup_limit == Decimal("5000")
    assert section_80d.health_insurance.preventive_checkup_within_category_limits


def test_hra_rules_define_three_limits_salary_and_metro_cities() -> None:
    rules = load_rules("2025-26")
    hra = next(d for d in rules.deductions if d.code == "HRA").hra

    assert hra is not None
    assert set(hra.exemption_is_minimum_of) == {
        "hra_received",
        "rent_paid_less_salary",
        "salary_percentage",
    }
    assert hra.salary_components == ("basic", "dearness_allowance")
    assert hra.rent_less_salary_rate == Decimal("0.10")
    assert hra.metro_salary_rate == Decimal("0.50")
    assert hra.non_metro_salary_rate == Decimal("0.40")
    assert "Mumbai" in hra.metro_cities


def test_hra_scenario_uses_city_instead_of_duplicate_metro_flag() -> None:
    scenarios_path = Path(__file__).parents[2] / "tests/fixtures/scenarios.json"
    data = json.loads(scenarios_path.read_text(encoding="utf-8"))
    scenario = next(
        item for item in data["scenarios"] if item["id"] == "03-metro-hra-old-wins"
    )

    assert scenario["inputs"]["city"] == "Mumbai"
    assert "metro" not in scenario["inputs"]
    assert scenario["inputs"]["deductions"]["section_80d_self_and_family"] == "60000"
    assert scenario["expected"]["old"]["section_80d_allowed"] == "25000"


def test_rule_sources_resolve_to_catalog_and_local_evidence() -> None:
    project_root = Path(__file__).parents[2]
    rules_path = project_root / "rules/fy2025_26.json"
    rules_data = json.loads(rules_path.read_text(encoding="utf-8"))
    with (project_root / "corpus/sources.csv").open(
        encoding="utf-8", newline=""
    ) as source_file:
        source_rows = list(csv.DictReader(source_file))
    sources = {row["source_id"]: row for row in source_rows}

    def collect_source_ids(value: object) -> set[str]:
        if isinstance(value, dict):
            ids = set(value.get("source_ids", []))
            for child in value.values():
                ids.update(collect_source_ids(child))
            return ids
        if isinstance(value, list):
            return set().union(*(collect_source_ids(child) for child in value))
        return set()

    referenced_sources = collect_source_ids(rules_data)
    assert referenced_sources <= sources.keys()
    assert {row["source_id"] for row in source_rows} >= referenced_sources
    for source_id in referenced_sources:
        local_path = sources[source_id]["local_path"]
        assert local_path
        assert (project_root / local_path).is_file()


def test_rules_reject_non_contiguous_tax_slabs() -> None:
    data = _read_rules_data()
    data["new_regime"]["slabs"][1]["min_income"] = "400001"

    with pytest.raises(ValidationError, match="does not start where"):
        TaxRules.model_validate(data)


def test_rules_reject_a_tax_slab_without_an_open_ended_top_band() -> None:
    data = _read_rules_data()
    data["old_regime"]["slabs"][-1]["max_income"] = "50000000"

    with pytest.raises(
        ValidationError, match="last old regime slab must have no upper limit"
    ):
        TaxRules.model_validate(data)


def test_rules_reject_rates_above_one() -> None:
    data = _read_rules_data()
    data["new_regime"]["slabs"][1]["rate"] = "1.01"

    with pytest.raises(ValidationError, match="less_than_equal"):
        TaxRules.model_validate(data)


def test_rules_reject_incomplete_hra_formula() -> None:
    data = _read_rules_data()
    data["deductions"][-1]["hra"]["exemption_is_minimum_of"].remove(
        "rent_paid_less_salary"
    )

    with pytest.raises(ValidationError, match="must use all three statutory limits"):
        TaxRules.model_validate(data)


def test_rules_reject_preventive_checkup_limit_above_80d_category_limits() -> None:
    data = _read_rules_data()
    data["deductions"][1]["health_insurance"]["preventive_checkup_limit"] = "30000"

    with pytest.raises(ValidationError, match="cannot exceed category limits"):
        TaxRules.model_validate(data)


def test_rules_reject_unknown_or_misspelled_fields() -> None:
    data = _read_rules_data()
    data["cess"]["raet"] = data["cess"].pop("rate")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaxRules.model_validate(data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("cess", "rate"), "4"),
        (("new_regime", "slabs", 1, "rate"), "5"),
        (("deductions", 5, "hra", "metro_salary_rate"), "50"),
    ],
)
def test_rules_reject_percentages_written_as_whole_numbers(
    path: tuple[str | int, ...], value: str
) -> None:
    data = _read_rules_data()
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError, match="less_than_equal"):
        TaxRules.model_validate(data)


@pytest.mark.parametrize("regime", ["new_regime", "old_regime"])
def test_rules_reject_empty_slab_lists(regime: str) -> None:
    data = _read_rules_data()
    data[regime]["slabs"] = []

    with pytest.raises(ValidationError, match="slabs cannot be empty"):
        TaxRules.model_validate(data)


@pytest.mark.parametrize(
    ("regime", "band_type", "first_rate", "second_rate"),
    [
        ("new_regime", "slabs", "0.10", "0.05"),
        ("old_regime", "surcharge_bands", "0.15", "0.10"),
    ],
)
def test_rules_reject_decreasing_bracket_rates(
    regime: str,
    band_type: str,
    first_rate: str,
    second_rate: str,
) -> None:
    data = _read_rules_data()
    bands = data[regime][band_type]
    bands[0]["rate"] = first_rate
    bands[1]["rate"] = second_rate

    with pytest.raises(ValidationError, match="rates cannot decrease"):
        TaxRules.model_validate(data)


def test_rules_reject_negative_amounts() -> None:
    data = _read_rules_data()
    data["new_regime"]["slabs"][0]["min_income"] = "-1"

    with pytest.raises(ValidationError, match="greater_than_equal"):
        TaxRules.model_validate(data)


def test_rules_reject_87a_rebate_above_its_income_limit() -> None:
    data = _read_rules_data()
    data["new_regime"]["rebate_87a"]["max_rebate"] = "1200001"

    with pytest.raises(ValidationError, match="cannot exceed its taxable-income limit"):
        TaxRules.model_validate(data)


def test_rules_reject_empty_source_id_lists() -> None:
    data = _read_rules_data()
    data["new_regime"]["slabs"][0]["source_ids"] = []

    with pytest.raises(ValidationError, match="at least 1 item"):
        TaxRules.model_validate(data)


def test_loader_rejects_source_ids_missing_from_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _read_rules_data()
    data["cess"]["source_ids"] = ["SOURCE-NOT-IN-CATALOG"]

    project_dir = tmp_path / "project"
    rules_dir = project_dir / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "fy2025_26.json").write_text(json.dumps(data), encoding="utf-8")
    corpus_dir = project_dir / "corpus"
    corpus_dir.mkdir()
    source_catalog = Path(__file__).parents[2] / "corpus/sources.csv"
    (corpus_dir / "sources.csv").write_text(
        source_catalog.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(
        json_loader, "get_settings", lambda: SimpleNamespace(rules_dir=rules_dir)
    )
    load_rules.cache_clear()

    try:
        with pytest.raises(
            ValueError, match="unknown source_ids.*SOURCE-NOT-IN-CATALOG"
        ):
            load_rules("2025-26")
    finally:
        load_rules.cache_clear()


def test_rules_reject_duplicate_deduction_codes() -> None:
    data = _read_rules_data()
    duplicate = data["deductions"][0].copy()
    duplicate["code"] = "80c"
    data["deductions"].append(duplicate)

    with pytest.raises(ValidationError, match="deduction codes must be unique"):
        TaxRules.model_validate(data)
