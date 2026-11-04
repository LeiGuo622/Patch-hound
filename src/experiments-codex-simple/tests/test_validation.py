from __future__ import annotations

import sys
import unittest
from pathlib import Path

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from simple_agent.validation import build_contract_repair_prompt, validate_final_response  # noqa: E402


def no_issues() -> str:
    return """# Patch Security Analysis

- Final Assessment: NO_NEW_ISSUES
- Confidence: HIGH

## Patch Summary

The patch changes one checked operation.

## New Security Issues

None.

## Limitations

None.
"""


def new_issue() -> str:
    return """# Patch Security Analysis

- Final Assessment: NEW_ISSUES_FOUND
- Confidence: MEDIUM

## Patch Summary

The patch changes validation.

## New Security Issues

1. **Unchecked length**
   - Location: `drivers/test.c`, `parse()`
   - Description: The patch removes a required bound check.
   - Impact: An attacker can write beyond the destination buffer.

## Limitations

None.
"""


class ValidationTest(unittest.TestCase):
    def test_accepts_no_issues_contract(self) -> None:
        result = validate_final_response(no_issues())
        self.assertTrue(result.ok)
        self.assertEqual(result.assessment, "NO_NEW_ISSUES")
        self.assertEqual(result.confidence, "HIGH")

    def test_accepts_numbered_finding_contract(self) -> None:
        result = validate_final_response(new_issue())
        self.assertTrue(result.ok)
        self.assertEqual(result.assessment, "NEW_ISSUES_FOUND")

    def test_accepts_multiple_findings(self) -> None:
        second = """

2. **Missing authorization**
   - Location: `drivers/test.c`, `update()`
   - Description: The patch makes an operation available without the former capability check.
   - Impact: An unprivileged caller can modify protected state.
"""
        text = new_issue().replace("\n## Limitations", second + "\n## Limitations")
        self.assertTrue(validate_final_response(text).ok)

    def test_rejects_missing_finding_field_and_extra_section(self) -> None:
        text = new_issue().replace("   - Impact: An attacker can write beyond the destination buffer.\n", "")
        text += "\n## Security Reasoning\n\nextra\n"
        result = validate_final_response(text)
        codes = {issue.code for issue in result.issues}
        self.assertFalse(result.ok)
        self.assertIn("MISSING_FINDING_FIELD", codes)
        self.assertIn("INVALID_SECTIONS", codes)

    def test_rejects_no_issues_with_findings(self) -> None:
        result = validate_final_response(no_issues().replace("None.", "1. **Maybe**", 1))
        self.assertFalse(result.ok)
        self.assertIn("NO_ISSUES_CONTENT", {issue.code for issue in result.issues})

    def test_rejects_invalid_assessment_and_missing_confidence(self) -> None:
        text = no_issues().replace("NO_NEW_ISSUES", "MAYBE").replace("- Confidence: HIGH\n", "")
        codes = {issue.code for issue in validate_final_response(text).issues}
        self.assertIn("INVALID_ASSESSMENT", codes)
        self.assertIn("CONFIDENCE_COUNT", codes)

    def test_rejects_new_issue_without_numbered_finding(self) -> None:
        text = new_issue().replace(
            "1. **Unchecked length**\n"
            "   - Location: `drivers/test.c`, `parse()`\n"
            "   - Description: The patch removes a required bound check.\n"
            "   - Impact: An attacker can write beyond the destination buffer.",
            "The patch might be unsafe.",
        )
        result = validate_final_response(text)
        self.assertFalse(result.ok)
        self.assertIn("MISSING_FINDINGS", {issue.code for issue in result.issues})

    def test_repair_prompt_reports_only_contract_errors(self) -> None:
        result = validate_final_response("hello")
        prompt = build_contract_repair_prompt(result)
        self.assertIn("INVALID_TITLE", prompt)
        self.assertIn("Return the complete corrected report only", prompt)
        self.assertIn("## Limitations", prompt)


if __name__ == "__main__":
    unittest.main()
