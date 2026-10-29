"""The `itr compute` command."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from itr.interfaces.cli.main import format_amount, main


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(["compute", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _row(output: str, label: str) -> list[str]:
    """The two amounts printed on the row that starts with `label`."""
    line = next(line for line in output.splitlines() if line.startswith(label))
    return line[len(label) :].split()


def test_scenario_3_sheet(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, "--scenario", "03")

    assert code == 0
    assert err == ""
    assert "Scenario 03-metro-hra-old-wins, FY 2025-26" in out
    assert _row(out, "Less: HRA exemption, 10(13A)") == ["₹0", "₹4,00,000"]
    assert _row(out, "Total income  ") == ["₹14,25,000", "₹8,75,000"]
    assert _row(out, "Tax payable (288B)") == ["₹97,500", "₹91,000"]
    assert out.rstrip().endswith("Old regime is cheaper by ₹6,500.")


@pytest.mark.parametrize("name", ["S3", "s3", "3", "03", "03-metro-hra-old-wins"])
def test_scenario_can_be_named_several_ways(
    capsys: pytest.CaptureFixture[str], name: str
) -> None:
    code, out, _ = _run(capsys, "--scenario", name)
    assert code == 0
    assert "Scenario 03-metro-hra-old-wins" in out


def test_negative_house_property_shows_minus_before_rupee(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, out, _ = _run(capsys, "--scenario", "04")
    assert _row(out, "House property, 24(b)") == ["₹0", "-₹2,00,000"]


def test_tie_is_reported(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"salary": {"gross": "500000"}}), encoding="utf-8")

    code, out, _ = _run(capsys, "--input", str(inputs))

    assert code == 0
    assert f"Inputs from {inputs}" in out
    assert out.rstrip().endswith("Both regimes cost the same.")


def test_unknown_scenario_fails_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, "--scenario", "99")
    assert code == 1
    assert out == ""
    assert "no single scenario matches '99'" in err


def test_bad_input_file_fails_cleanly(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"salary": {"gross": "-1"}}), encoding="utf-8")

    code, _, err = _run(capsys, "--input", str(inputs))

    assert code == 1
    assert "greater than or equal to 0" in err


def test_scenario_or_input_is_required() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["compute"])
    assert exit_info.value.code == 2  # argparse's usage error


@pytest.mark.parametrize(
    ("amount", "shown"),
    [
        ("0", "₹0"),
        ("-0", "₹0"),
        ("97500", "₹97,500"),
        ("-200000", "-₹2,00,000"),
        ("6300.1200", "₹6,300.12"),
        ("400.0000", "₹400"),
        ("10.40", "₹10.40"),
    ],
)
def test_format_amount(amount: str, shown: str) -> None:
    assert format_amount(Decimal(amount)) == shown
