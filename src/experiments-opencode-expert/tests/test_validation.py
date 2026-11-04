from __future__ import annotations

import sys
from pathlib import Path

from conftest import VALID_NEW_ISSUE, VALID_NO_ISSUES

from opencode_expert_agent.validation import build_contract_repair_prompt, validate_final_response

SIMPLE_ROOT = Path(__file__).resolve().parents[2] / "experiments-codex-simple"
sys.path.insert(0, str(SIMPLE_ROOT))
from simple_agent.validation import validate_final_response as baseline_validate  # noqa: E402


def test_accepts_both_valid_assessments() -> None:
    assert validate_final_response(VALID_NO_ISSUES).ok
    assert validate_final_response(VALID_NEW_ISSUE).ok


def test_validation_matches_experiments_codex_simple() -> None:
    samples = [
        "",
        "hello",
        VALID_NO_ISSUES,
        VALID_NEW_ISSUE,
        VALID_NO_ISSUES.replace("NO_NEW_ISSUES", "MAYBE"),
        VALID_NEW_ISSUE.replace("   - Impact: An attacker can write beyond the destination buffer.\n", ""),
        VALID_NO_ISSUES + "\n## Extra\n\nNo.\n",
    ]
    for sample in samples:
        ours = validate_final_response(sample)
        baseline = baseline_validate(sample)
        assert (ours.ok, ours.assessment, ours.confidence) == (baseline.ok, baseline.assessment, baseline.confidence)
        assert [issue.code for issue in ours.issues] == [issue.code for issue in baseline.issues]


def test_repair_prompt_contains_contract_errors_but_not_previous_answer() -> None:
    invalid = "previous sensitive answer"
    prompt = build_contract_repair_prompt(validate_final_response(invalid))
    assert "INVALID_TITLE" in prompt
    assert invalid not in prompt
    assert "Return the complete corrected report only" in prompt
