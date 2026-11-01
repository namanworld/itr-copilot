"""Measure extraction: run every document, compare each field with its truth.

evaluate_document() runs one extraction and scores it; build_report() turns
the results into the JSON report. Neither touches files or the network: the
CLI reads the PDFs and truth files and passes in the LLM client, so tests
can use FakeLLM.

How documents are counted:

- A document whose extraction fails (the reply didn't fit the schema, or the
  rows or the domain rejected it) counts as wrong on every field. A failed
  extraction gives the user nothing, so it must lower accuracy, not vanish
  from it.
- Costs are what each call cost when it was made, so a re-run served from
  the disk cache still reports what a fresh run would cost. charged_usd is
  what this run actually spent.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from itr.application.extract_form16 import (
    NOT_EXTRACTED,
    Form16ValidationError,
    extract_form16,
)
from itr.application.form16_diff import FieldDiff, diff_form16, field_paths
from itr.application.ports import LLMClient, LLMOutputError, LLMUsage
from itr.domain.form16 import Form16


@dataclass(frozen=True)
class DocumentResult:
    name: str
    fields: tuple[str, ...]  # every field compared for this document
    mismatches: tuple[FieldDiff, ...] = ()
    error: str | None = None  # set when the extraction failed
    usage: LLMUsage | None = None  # None when no reply came back at all
    problems: tuple[str, ...] = ()  # why validation failed

    @property
    def extracted(self) -> bool:
        return self.error is None

    @property
    def wrong_fields(self) -> set[str]:
        if not self.extracted:
            return set(self.fields)
        return {m.path for m in self.mismatches}


def evaluate_document(
    name: str, text: str, truth: Form16, llm: LLMClient, system_prompt: str
) -> DocumentResult:
    fields = tuple(field_paths(truth, skip=NOT_EXTRACTED))
    try:
        extraction = extract_form16(text, llm, system_prompt)
    except Form16ValidationError as error:
        return DocumentResult(
            name,
            fields,
            error="extracted Form 16 failed validation",
            usage=error.usage,
            problems=tuple(error.problems),
        )
    except LLMOutputError as error:
        return DocumentResult(name, fields, error=str(error))

    diff = diff_form16(truth, extraction.form16, skip=NOT_EXTRACTED)
    return DocumentResult(name, fields, diff.mismatches, usage=extraction.usage)


def _money(value: Decimal) -> str:
    return f"{value:.6f}"


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def build_report(results: list[DocumentResult], run: dict[str, Any]) -> dict[str, Any]:
    """The report as a JSON-ready dict. `run` holds facts about the run
    (date, model, prompt) and is copied in as is."""
    documents = len(results)
    all_fields = sorted({f for r in results for f in r.fields})

    field_accuracy: dict[str, dict[str, Any]] = {}
    for path in all_fields:
        scored = [r for r in results if path in r.fields]
        correct = sum(1 for r in scored if path not in r.wrong_fields)
        field_accuracy[path] = {
            "correct": correct,
            "documents": len(scored),
            "accuracy": _share(correct, len(scored)),
        }

    fully_correct = [r.name for r in results if r.extracted and not r.mismatches]
    usages = [r.usage for r in results if r.usage is not None]
    total = sum((u.cost_usd for u in usages), Decimal(0))
    charged = sum((u.cost_usd for u in usages if not u.from_disk_cache), Decimal(0))

    return {
        **run,
        "documents": documents,
        "documents_extracted": sum(1 for r in results if r.extracted),
        "documents_fully_correct": len(fully_correct),
        "fully_correct_share": _share(len(fully_correct), documents),
        "field_accuracy": field_accuracy,
        "fields_below_100_percent": [
            path for path, score in field_accuracy.items() if score["accuracy"] < 1
        ],
        "failures": [
            {
                "document": r.name,
                "field": m.path,
                "expected": None if m.expected is None else str(m.expected),
                "extracted": None if m.actual is None else str(m.actual),
            }
            for r in results
            for m in r.mismatches
        ],
        "failed_extractions": [
            {"document": r.name, "error": r.error, "problems": list(r.problems)}
            for r in results
            if not r.extracted
        ],
        "cost": {
            "total_usd": _money(total),
            "average_usd_per_document": _money(total / len(usages))
            if usages
            else _money(Decimal(0)),
            "charged_this_run_usd": _money(charged),
            "documents_from_disk_cache": sum(1 for u in usages if u.from_disk_cache),
            "tokens": {
                "input": sum(u.input_tokens for u in usages),
                "output": sum(u.output_tokens for u in usages),
                "cache_write": sum(u.cache_write_tokens for u in usages),
                "cache_read": sum(u.cache_read_tokens for u in usages),
            },
        },
    }
