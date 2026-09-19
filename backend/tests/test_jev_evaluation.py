"""Evaluation reports compare Jev routes with the configured chat LLM."""
from __future__ import annotations

from pathlib import Path

from app.jev.evaluation import EvalPrediction, evaluate_cases, load_cases, render_report


def test_evaluation_reports_language_metrics_and_only_passes_reviewed_routes():
    cases = [
        {"id": "en-ba", "language": "en", "message": "Write requirements", "expected": "business_analysis"},
        {"id": "en-summary", "language": "en", "message": "Summarize sources", "expected": "summarize_sources"},
        {"id": "en-chat", "language": "en", "message": "Explain auth", "expected": "general_question"},
        {"id": "th-ba", "language": "th", "message": "สร้าง requirements", "expected": "business_analysis"},
        {"id": "th-summary", "language": "th", "message": "สรุปเอกสาร", "expected": "summarize_sources"},
        {"id": "th-clarify", "language": "th", "message": "ทำให้ดีขึ้น", "expected": "needs_clarification"},
    ]
    predictions = {
        "en-ba": EvalPrediction("business_analysis", 0.99, 10, {"input_tokens": 100}),
        "en-summary": EvalPrediction("summarize_sources", 0.98, 10, {"input_tokens": 100}),
        "en-chat": EvalPrediction("general_question", 0.80, 11, {"input_tokens": 100}),
        "th-ba": EvalPrediction("business_analysis", 0.99, 12, {"input_tokens": 100}),
        "th-summary": EvalPrediction("summarize_sources", 0.98, 12, {"input_tokens": 100}),
        "th-clarify": EvalPrediction("needs_clarification", 0.97, 13, {"input_tokens": 100}),
    }
    baseline = {case["id"]: case["expected"] for case in cases}
    predict = lambda message: next(
        predictions[case["id"]] for case in cases if case["message"] == message
    )
    baseline_predict = lambda message: next(
        baseline[case["id"]] for case in cases if case["message"] == message
    )

    report = evaluate_cases(
        cases, predict, baseline_predict, human_reviewed=True,
    )

    assert report["case_count"] == 6
    assert report["jev"]["by_language"]["en"]["accuracy"] == 1.0
    assert report["jev"]["by_language"]["th"]["accuracy"] == 1.0
    assert report["jev"]["auto_routes"]["th"]["summarize_sources"]["precision"] == 1.0
    assert report["calibration"]["jev"]["en"]["expected_calibration_error"] == 0.076667
    assert report["calibration"]["baseline"]["en"]["expected_calibration_error"] is None
    assert report["acceptance"]["passed"] is True


def test_calibration_reports_brier_score_from_choice_probabilities():
    cases = [{
        "id": "one", "language": "en", "message": "hello",
        "expected": "general_question",
    }]
    probabilities = {
        "business_analysis": 0.1,
        "summarize_sources": 0.1,
        "general_question": 0.7,
        "needs_clarification": 0.1,
    }

    report = evaluate_cases(
        cases,
        lambda _message: EvalPrediction(
            "general_question", 0.7, probabilities=probabilities,
        ),
        lambda _message: "general_question",
    )

    assert report["calibration"]["jev"]["en"]["brier_score"] == 0.12
    assert report["calibration"]["jev"]["en"]["brier_count"] == 1


def test_rendered_report_includes_precision_and_recall_for_every_category():
    cases = [{
        "id": "one", "language": "th", "message": "สวัสดี",
        "expected": "general_question",
    }]
    report = evaluate_cases(cases, lambda _message: "general_question", lambda _message: "general_question")

    rendered = render_report(report, "sample")

    assert "## Class-wise precision and recall" in rendered
    assert "| th | general_question |" in rendered
    assert "| th | needs_clarification |" in rendered
    assert "Recommendation: keep automatic Jev routing off" in rendered


def test_evaluation_reports_chat_baseline_token_usage():
    cases = [{
        "id": "one", "language": "en", "message": "hello",
        "expected": "general_question",
    }]

    report = evaluate_cases(
        cases,
        lambda _message: EvalPrediction("general_question"),
        lambda _message: EvalPrediction(
            "general_question", usage={"input_tokens": 20, "output_tokens": 2},
        ),
    )

    assert report["baseline"]["usage"] == {"input_tokens": 20, "output_tokens": 2}
    assert report["baseline"]["estimated_cost_usd"] is None


def test_unreviewed_or_low_precision_data_does_not_pass_acceptance():
    cases = [
        {"id": "th-ba", "language": "th", "message": "สร้าง requirements", "expected": "business_analysis"},
        {"id": "th-other", "language": "th", "message": "อธิบายศัพท์", "expected": "general_question"},
        {"id": "en-summary", "language": "en", "message": "Summarize sources", "expected": "summarize_sources"},
        {"id": "en-other", "language": "en", "message": "What is OAuth?", "expected": "general_question"},
    ]
    responses = iter([
        EvalPrediction("business_analysis", 0.99),
        EvalPrediction("business_analysis", 0.99),  # false skill route
        EvalPrediction("summarize_sources", 0.99),
        EvalPrediction("general_question", 0.80),
    ])

    report = evaluate_cases(
        cases, lambda _message: next(responses), lambda _message: "general_question",
        human_reviewed=False,
    )

    assert report["acceptance"]["passed"] is False
    assert report["acceptance"]["reason"].startswith("evaluation set is not human reviewed")
    assert report["jev"]["auto_routes"]["th"]["business_analysis"]["precision"] == 0.5


def test_evaluation_dataset_validation_rejects_unknown_labels(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(
        '{"cases":[{"id":"x","language":"en","message":"hello",'
        '"expected":"run_tool"}]}', encoding="utf-8",
    )

    try:
        load_cases(path)
    except ValueError as exc:
        assert "unsupported expected label" in str(exc)
    else:
        raise AssertionError("unsupported category was accepted")


def test_bundled_evaluation_set_is_explicitly_unreviewed():
    dataset = load_cases(Path(__file__).parents[1] / "app/jev/evaluation_cases.json")

    assert len(dataset["cases"]) == 32
    assert dataset["human_reviewed"] is False
    assert {case["language"] for case in dataset["cases"]} == {"en", "th"}
