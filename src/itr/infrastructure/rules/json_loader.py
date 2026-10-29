import json
from functools import cache
from typing import Never

from itr.domain.rules import TaxRules
from itr.settings import get_settings


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
    return TaxRules.model_validate(data)
