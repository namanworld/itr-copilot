import csv
import json
from functools import cache
from pathlib import Path
from typing import Never

from itr.domain.rules import TaxRules
from itr.settings import get_settings


def _validate_source_references(rules_data: object, catalog_path: Path) -> None:
    try:
        with catalog_path.open(encoding="utf-8", newline="") as source_file:
            reader = csv.DictReader(source_file)
            if reader.fieldnames is None or "source_id" not in reader.fieldnames:
                raise ValueError(f"{catalog_path} must have a source_id column")
            catalog_ids = {
                row["source_id"].strip()
                for row in reader
                if row.get("source_id") and row["source_id"].strip()
            }
    except OSError as error:
        raise ValueError(f"unable to read source catalog {catalog_path}") from error

    def collect_ids(value: object) -> set[str]:
        if isinstance(value, dict):
            ids = set(value.get("source_ids", []))
            for child in value.values():
                ids.update(collect_ids(child))
            return ids
        if isinstance(value, list):
            return set().union(*(collect_ids(child) for child in value))
        return set()

    unknown_ids = collect_ids(rules_data) - catalog_ids
    if unknown_ids:
        formatted_ids = ", ".join(sorted(unknown_ids))
        raise ValueError(f"unknown source_ids in rules file: {formatted_ids}")


def _reject_float(text: str) -> Never:
    # json.loads calls this for every unquoted decimal number, e.g. 0.05.
    # Amounts and rates must be written as strings ("0.05") so they become
    # exact Decimals, so we refuse floats instead of converting them.
    raise ValueError(f'unquoted number {text} in rules file; write it as "{text}"')


@cache
def load_rules(fy: str) -> TaxRules:
    """
    Read, validate and cache the rules for one financial year, e.g. "2025-26".

    The result is cached by `fy` only. If a test changes the rules folder in
    Settings, it must call load_rules.cache_clear(), or it will keep getting
    the rules loaded from the old folder.
    """
    path = get_settings().rules_dir / f"fy{fy.replace('-', '_')}.json"
    data = json.loads(path.read_text(encoding="utf-8"), parse_float=_reject_float)
    rules = TaxRules.model_validate(data)
    _validate_source_references(data, path.parent.parent / "corpus" / "sources.csv")
    return rules
