"""Field-by-field comparison of an extracted Form16 with its truth.

Used by `itr extract` now and by the evals later. Leaves are compared as
Python values, so amounts compare as Decimals: 123456.00 printed on the form
equals 123456 in the truth, while "000123" and "123" stay different strings.
"""

from dataclasses import dataclass
from typing import Any

from itr.domain.form16 import Form16


@dataclass(frozen=True)
class FieldDiff:
    path: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class Form16Diff:
    compared: int
    mismatches: tuple[FieldDiff, ...]

    @property
    def matched(self) -> int:
        return self.compared - len(self.mismatches)


def _leaves(node: Any, path: str = "") -> dict[str, Any]:
    """Every value in a dumped model, keyed by its dotted path."""
    if isinstance(node, dict):
        leaves: dict[str, Any] = {}
        for key, value in node.items():
            leaves |= _leaves(value, f"{path}.{key}" if path else key)
        return leaves
    if isinstance(node, (list, tuple)) and node:
        leaves = {}
        for index, value in enumerate(node):
            leaves |= _leaves(value, f"{path}[{index}]")
        return leaves
    return {path: node}


def _skipped(path: str, skip: tuple[str, ...]) -> bool:
    return any(
        path == prefix or path.startswith((f"{prefix}.", f"{prefix}["))
        for prefix in skip
    )


def field_paths(form16: Form16, skip: tuple[str, ...] = ()) -> list[str]:
    """Every compared field of a Form16, as dotted paths, except under `skip`."""
    return sorted(
        path for path in _leaves(form16.model_dump()) if not _skipped(path, skip)
    )


def diff_form16(
    truth: Form16, extracted: Form16, skip: tuple[str, ...] = ()
) -> Form16Diff:
    """Compare every leaf, except paths under any prefix in `skip`."""
    expected = _leaves(truth.model_dump())
    actual = _leaves(extracted.model_dump())
    paths = sorted(
        path for path in expected.keys() | actual.keys() if not _skipped(path, skip)
    )
    mismatches = tuple(
        FieldDiff(path, expected.get(path), actual.get(path))
        for path in paths
        if expected.get(path) != actual.get(path)
    )
    return Form16Diff(compared=len(paths), mismatches=mismatches)
