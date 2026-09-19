"""Run a local Jev-vs-chat-model evaluation without retaining prompts."""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .client import INTENT_OPTIONS, JevDecision, JevDecisionClient, MODEL_ID
from .routing import INTENT_SKILLS, MIN_AUTO_ROUTE_CONFIDENCE
from .settings import SECRET_REF

JEV_INPUT_USD_PER_MILLION = 0.042
BASELINE_INSTRUCTIONS = (
    "Classify only the user's requested task. Choose one category from the "
    "provided list and return only its exact identifier, with no explanation."
)


@dataclass(frozen=True)
class EvalPrediction:
    category: str
    confidence: float | None = None
    latency_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    probabilities: dict[str, float] = field(default_factory=dict)


def load_cases(path: str | Path) -> dict:
    """Load a reviewable evaluation file and reject labels outside the schema."""
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("could not read the Jev evaluation set") from exc
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("evaluation set must contain a non-empty cases list")

    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("each evaluation case must be an object")
        case_id = case.get("id")
        language = case.get("language")
        message = case.get("message")
        expected = case.get("expected")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("evaluation case IDs must be unique non-empty strings")
        seen.add(case_id)
        if language not in ("en", "th"):
            raise ValueError(f"unsupported evaluation language for case {case_id}")
        if not isinstance(message, str) or not message.strip():
            raise ValueError(f"evaluation case {case_id} has no message")
        if expected not in INTENT_OPTIONS:
            raise ValueError(f"unsupported expected label for case {case_id}")
    return {
        "cases": cases,
        "human_reviewed": data.get("human_reviewed") is True,
        "dataset_name": data.get("dataset_name") or source.stem,
    }


def _normalize_prediction(value) -> EvalPrediction:
    if isinstance(value, EvalPrediction):
        result = value
    elif isinstance(value, JevDecision):
        result = EvalPrediction(
            value.category, value.confidence, value.latency_ms, value.usage,
            value.probabilities,
        )
    elif isinstance(value, str):
        result = EvalPrediction(value)
    else:
        raise ValueError("evaluation predictor returned an unsupported value")
    if result.category not in INTENT_OPTIONS:
        raise ValueError("evaluation predictor returned an unsupported category")
    if result.confidence is not None and (
        isinstance(result.confidence, bool)
        or not isinstance(result.confidence, (int, float))
        or not math.isfinite(result.confidence)
        or not 0 <= result.confidence <= 1
    ):
        raise ValueError("evaluation predictor returned invalid confidence")
    if result.probabilities:
        if set(result.probabilities) != set(INTENT_OPTIONS) or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
            for value in result.probabilities.values()
        ):
            raise ValueError("evaluation predictor returned invalid probabilities")
    return result


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _latency_summary(values: list[int]) -> dict:
    if not values:
        return {"p50_ms": None, "p95_ms": None}
    ordered = sorted(values)
    p50 = statistics.median(ordered)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return {"p50_ms": round(p50), "p95_ms": p95}


def _metrics_for_language(rows: list[dict], method: str) -> dict:
    predicted = [row[method] for row in rows]
    valid = [item for item in predicted if item is not None]
    correct = sum(
        item["category"] == row["expected"]
        for row, item in zip(rows, predicted) if item is not None
    )
    category_metrics = {}
    for category in INTENT_OPTIONS:
        tp = sum(
            item is not None and item["category"] == category
            and row["expected"] == category
            for row, item in zip(rows, predicted)
        )
        fp = sum(
            item is not None and item["category"] == category
            and row["expected"] != category
            for row, item in zip(rows, predicted)
        )
        fn = sum(
            row["expected"] == category
            and (item is None or item["category"] != category)
            for row, item in zip(rows, predicted)
        )
        precision = _ratio(tp, tp + fp)
        recall = _ratio(tp, tp + fn)
        f1 = None
        if precision is not None and recall is not None and precision + recall:
            f1 = round(2 * precision * recall / (precision + recall), 4)
        category_metrics[category] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": sum(row["expected"] == category for row in rows),
        }
    return {
        "case_count": len(rows),
        "coverage": _ratio(len(valid), len(rows)),
        "accuracy": _ratio(correct, len(rows)),
        "categories": category_metrics,
    }


def _calibration_metrics(rows: list[dict], method: str) -> dict:
    """Report top-label ECE and multiclass Brier when probabilities are available."""
    confident = [
        (row, row[method]) for row in rows
        if row[method] is not None and row[method].get("confidence") is not None
    ]
    if not confident:
        return {"count": 0, "expected_calibration_error": None, "brier_score": None}

    bins = []
    weighted_error = 0.0
    for bin_index in range(10):
        lower = bin_index / 10
        upper = (bin_index + 1) / 10
        members = [
            (row, item) for row, item in confident
            if lower <= item["confidence"] < upper
            or (bin_index == 9 and item["confidence"] == 1)
        ]
        if not members:
            continue
        mean_confidence = sum(item["confidence"] for _, item in members) / len(members)
        accuracy = sum(item["category"] == row["expected"] for row, item in members) / len(members)
        weighted_error += len(members) / len(confident) * abs(mean_confidence - accuracy)
        bins.append({
            "range": f"{lower:.1f}-{upper:.1f}",
            "count": len(members),
            "mean_confidence": round(mean_confidence, 4),
            "accuracy": round(accuracy, 4),
        })

    probability_rows = [
        (row, item) for row, item in confident
        if set(item.get("probabilities", {})) == set(INTENT_OPTIONS)
    ]
    brier = None
    if probability_rows:
        total = 0.0
        for row, item in probability_rows:
            total += sum(
                (item["probabilities"][category] - (category == row["expected"])) ** 2
                for category in INTENT_OPTIONS
            )
        brier = round(total / len(probability_rows), 6)

    return {
        "count": len(confident),
        "expected_calibration_error": round(weighted_error, 6),
        "brier_score": brier,
        "brier_count": len(probability_rows),
        "bins": bins,
    }


def _route_metrics(rows: list[dict], method: str, threshold: float) -> dict:
    metrics = {}
    for category in INTENT_SKILLS:
        tp = fp = fn = 0
        for row in rows:
            item = row[method]
            route = None
            if item and item["category"] == category:
                confidence = item.get("confidence")
                if confidence is None or confidence >= threshold:
                    route = category
            expected_route = row["expected"] == category
            predicted_route = route == category
            if predicted_route and expected_route:
                tp += 1
            elif predicted_route:
                fp += 1
            elif expected_route:
                fn += 1
        metrics[category] = {
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "expected_count": sum(row["expected"] == category for row in rows),
        }
    return metrics


def evaluate_cases(
    cases: list[dict],
    jev_predict: Callable[[str], JevDecision | EvalPrediction],
    baseline_predict: Callable[[str], str | EvalPrediction],
    *,
    human_reviewed: bool = False,
    threshold: float = MIN_AUTO_ROUTE_CONFIDENCE,
) -> dict:
    """Measure Jev and chat-model labels, including high-confidence skill routes."""
    rows = []
    latency = {"jev": [], "baseline": []}
    usage = {"input_tokens": 0, "output_tokens": 0}
    baseline_usage = {"input_tokens": 0, "output_tokens": 0}
    errors = {"jev": 0, "baseline": 0}

    for case in cases:
        row = {"id": case["id"], "language": case["language"], "expected": case["expected"]}
        try:
            prediction = _normalize_prediction(jev_predict(case["message"]))
            row["jev"] = {
                "category": prediction.category,
                "confidence": prediction.confidence,
                "probabilities": prediction.probabilities,
            }
            latency["jev"].append(prediction.latency_ms)
            for key in usage:
                usage[key] += prediction.usage.get(key, 0)
        except Exception:
            errors["jev"] += 1
            row["jev"] = None

        try:
            prediction = _normalize_prediction(baseline_predict(case["message"]))
            row["baseline"] = {
                "category": prediction.category,
                "confidence": prediction.confidence,
                "probabilities": prediction.probabilities,
            }
            latency["baseline"].append(prediction.latency_ms)
            for key in baseline_usage:
                baseline_usage[key] += prediction.usage.get(key, 0)
        except Exception:
            errors["baseline"] += 1
            row["baseline"] = None
        rows.append(row)

    languages = sorted({row["language"] for row in rows})
    jev_by_language = {}
    baseline_by_language = {}
    jev_routes = {}
    baseline_routes = {}
    acceptance_routes = []
    for language in languages:
        language_rows = [row for row in rows if row["language"] == language]
        jev_by_language[language] = _metrics_for_language(language_rows, "jev")
        baseline_by_language[language] = _metrics_for_language(language_rows, "baseline")
        jev_routes[language] = _route_metrics(language_rows, "jev", threshold)
        baseline_routes[language] = _route_metrics(language_rows, "baseline", threshold)
        for category in INTENT_SKILLS:
            metric = jev_routes[language][category]
            acceptance_routes.append(
                metric["expected_count"] > 0
                and metric["precision"] is not None
                and metric["precision"] >= 0.95
            )

    calibration = {
        "jev": {
            language: _calibration_metrics(
                [row for row in rows if row["language"] == language], "jev",
            )
            for language in languages
        },
        "baseline": {
            language: _calibration_metrics(
                [row for row in rows if row["language"] == language], "baseline",
            )
            for language in languages
        },
    }

    gate_passed = bool(acceptance_routes) and all(acceptance_routes)
    reasons = []
    if not human_reviewed:
        reasons.append("evaluation set is not human reviewed")
    if not gate_passed:
        reasons.append("one or more language/skill routes missed 95% precision or have no expected examples")

    return {
        "model": MODEL_ID,
        "threshold": threshold,
        "case_count": len(rows),
        "human_reviewed": human_reviewed,
        "jev": {
            "by_language": jev_by_language,
            "auto_routes": jev_routes,
            "latency": _latency_summary(latency["jev"]),
            "errors": errors["jev"],
            "usage": usage,
            "estimated_cost_usd": round(
                usage["input_tokens"] * JEV_INPUT_USD_PER_MILLION / 1_000_000, 8
            ),
        },
        "baseline": {
            "by_language": baseline_by_language,
            "auto_routes": baseline_routes,
            "latency": _latency_summary(latency["baseline"]),
            "errors": errors["baseline"],
            "usage": baseline_usage,
            # OpenAI-compatible profiles do not carry normalized price metadata.
            "estimated_cost_usd": None,
        },
        "calibration": calibration,
        "acceptance": {
            "minimum_route_precision": 0.95,
            "passed": human_reviewed and gate_passed,
            "reason": "; ".join(reasons) if reasons else "all reviewed skill routes meet the precision gate",
        },
        # IDs and decisions only; the source request text is never copied into the report.
        "cases": rows,
    }


def render_report(report: dict, dataset_name: str) -> str:
    lines = [
        "# Jev routing evaluation",
        "",
        f"- Dataset: {dataset_name}",
        f"- Cases: {report['case_count']}",
        f"- Jev model: `{report['model']}`",
        f"- Dataset human reviewed: {'yes' if report['human_reviewed'] else 'no'}",
        f"- Auto-route confidence threshold: {report['threshold']:.2f}",
        "",
        "## Classification accuracy",
        "",
        "| Language | Jev accuracy | Jev coverage | Baseline accuracy | Baseline coverage |",
        "|---|---:|---:|---:|---:|",
    ]
    for language in sorted(report["jev"]["by_language"]):
        jev = report["jev"]["by_language"][language]
        baseline = report["baseline"]["by_language"][language]
        lines.append(
            f"| {language} | {_display(jev['accuracy'])} | {_display(jev['coverage'])} | "
            f"{_display(baseline['accuracy'])} | {_display(baseline['coverage'])} |"
        )
    lines.extend([
        "",
        "## Class-wise precision and recall",
        "",
        "| Language | Category | Jev precision | Jev recall | Baseline precision | Baseline recall |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for language in sorted(report["jev"]["by_language"]):
        jev_categories = report["jev"]["by_language"][language]["categories"]
        baseline_categories = report["baseline"]["by_language"][language]["categories"]
        for category in INTENT_OPTIONS:
            jev = jev_categories[category]
            baseline = baseline_categories[category]
            lines.append(
                f"| {language} | {category} | {_display(jev['precision'])} | "
                f"{_display(jev['recall'])} | {_display(baseline['precision'])} | "
                f"{_display(baseline['recall'])} |"
            )
    lines.extend([
        "",
        "## Confidence calibration",
        "",
        "| Language | Jev ECE | Jev Brier | Baseline ECE | Baseline Brier |",
        "|---|---:|---:|---:|---:|",
    ])
    for language in sorted(report["calibration"]["jev"]):
        jev = report["calibration"]["jev"][language]
        baseline = report["calibration"]["baseline"][language]
        lines.append(
            f"| {language} | {_display(jev['expected_calibration_error'])} | "
            f"{_display(jev['brier_score'])} | "
            f"{_display(baseline['expected_calibration_error'])} | "
            f"{_display(baseline['brier_score'])} |"
        )
    lines.extend([
        "",
        "## Automatic skill routes",
        "",
        "| Language | Route | Precision | Recall | TP | FP | FN |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for language in sorted(report["jev"]["auto_routes"]):
        for category, metric in report["jev"]["auto_routes"][language].items():
            lines.append(
                f"| {language} | {category} | {_display(metric['precision'])} | "
                f"{_display(metric['recall'])} | {metric['true_positive']} | "
                f"{metric['false_positive']} | {metric['false_negative']} |"
            )
    lines.extend([
        "",
        "## Runtime and cost",
        "",
        f"- Jev latency p50/p95: {report['jev']['latency']['p50_ms']} / {report['jev']['latency']['p95_ms']} ms",
        f"- Baseline latency p50/p95: {report['baseline']['latency']['p50_ms']} / {report['baseline']['latency']['p95_ms']} ms",
        f"- Jev errors: {report['jev']['errors']}; baseline errors: {report['baseline']['errors']}",
        f"- Jev input/output tokens: {report['jev']['usage']['input_tokens']} / {report['jev']['usage']['output_tokens']}",
        f"- Estimated Jev input cost: ${report['jev']['estimated_cost_usd']:.8f} at the configured list price",
        f"- Chat baseline input/output tokens: {report['baseline']['usage']['input_tokens']} / {report['baseline']['usage']['output_tokens']}",
        "- Chat baseline cost: unavailable because the configured LLM profile does not provide normalized pricing metadata",
        "",
        "## Acceptance",
        "",
        f"**{'PASS' if report['acceptance']['passed'] else 'NOT PASSED'}** — {report['acceptance']['reason']}",
        (
            "Recommendation: consider opt-in routing only after reviewing the measured results."
            if report["acceptance"]["passed"]
            else "Recommendation: keep automatic Jev routing off until a human-reviewed evaluation passes."
        ),
        "",
        "The report stores case IDs and predictions, not request text or credentials. Jev price is an estimate and must be updated if TypeSafe changes its list price.",
        "",
    ])
    return "\n".join(lines)


def _display(value) -> str:
    return "—" if value is None else f"{value:.3f}"


def _baseline_category(text: str) -> str:
    value = text.strip().strip("`\"' .\n\t")
    if value in INTENT_OPTIONS:
        return value
    try:
        parsed = json.loads(value)
        value = parsed.get("category", "") if isinstance(parsed, dict) else ""
    except (json.JSONDecodeError, AttributeError):
        found = re.search(r"\b(" + "|".join(INTENT_OPTIONS) + r")\b", value)
        value = found.group(1) if found else ""
    return value if value in INTENT_OPTIONS else "__invalid_prediction__"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate Jev intent routing against the default chat model")
    parser.add_argument(
        "--cases", type=Path,
        default=Path(__file__).with_name("evaluation_cases.json"),
        help="JSON dataset file (defaults to the included reviewable sample set)",
    )
    parser.add_argument(
        "--output", type=Path,
        help="Report destination (defaults to the local app data directory)",
    )
    args = parser.parse_args(argv)

    try:
        dataset = load_cases(args.cases)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    from .. import db
    from ..config import Settings
    from ..llm import profiles
    from ..llm.base import ChatMessage
    from ..secrets import SecretStore

    app_settings = Settings()
    conn = db.connect(app_settings.db_path)
    jev_client = None
    baseline_client = None
    try:
        db.init_db(conn)
        secrets = SecretStore(app_settings.secrets_path, app_settings.secret_key_path)
        jev_key = secrets.get(SECRET_REF)
        if not jev_key:
            print("Save a TypeSafe API key in Settings before running the live evaluation.", file=sys.stderr)
            return 2
        profile = profiles.default_profile(conn)
        if not profile:
            print("Configure a default chat LLM profile before running the comparison.", file=sys.stderr)
            return 2
        jev_client = JevDecisionClient(jev_key)
        baseline_client = profiles.build_client(conn, secrets, profile)

        def jev_predict(message: str) -> JevDecision:
            return jev_client.classify(message)

        def baseline_predict(message: str) -> EvalPrediction:
            started = time.monotonic()
            result = baseline_client.complete([
                ChatMessage(role="system", content=(
                    BASELINE_INSTRUCTIONS + " Categories: " + ", ".join(INTENT_OPTIONS)
                )),
                ChatMessage(role="user", content=message),
            ], max_output_tokens=32)
            return EvalPrediction(
                _baseline_category(result.content or ""),
                latency_ms=int((time.monotonic() - started) * 1000),
                usage={
                    "input_tokens": result.prompt_tokens or 0,
                    "output_tokens": result.completion_tokens or 0,
                },
            )

        report = evaluate_cases(
            dataset["cases"], jev_predict, baseline_predict,
            human_reviewed=dataset["human_reviewed"],
        )
        report_text = render_report(report, dataset["dataset_name"])
        destination = args.output or (app_settings.data_dir / "jev-evaluation" / "latest.md")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(report_text, encoding="utf-8")
        print(report_text)
        print(f"\nReport written to {destination}")
        return 0
    finally:
        if jev_client:
            jev_client.close()
        if baseline_client:
            baseline_client.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
