"""Final Markdown response validation, kept equivalent to experiments-codex-simple."""

from __future__ import annotations

import re
from dataclasses import dataclass

EXPECTED_SECTIONS = ("Patch Summary", "New Security Issues", "Limitations")
ASSESSMENTS = {"NO_NEW_ISSUES", "NEW_ISSUES_FOUND"}
CONFIDENCE_VALUES = {"HIGH", "MEDIUM", "LOW"}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    assessment: str | None
    confidence: str | None
    issues: tuple[ValidationIssue, ...]


def validate_final_response(text: str) -> ValidationResult:
    issues: list[ValidationIssue] = []
    stripped = text.strip()
    if not stripped:
        return ValidationResult(False, None, None, (ValidationIssue("EMPTY_RESPONSE", "The final response is empty."),))
    lines = stripped.splitlines()
    first_nonempty = next((line.strip() for line in lines if line.strip()), "")
    if first_nonempty != "# Patch Security Analysis":
        issues.append(ValidationIssue("INVALID_TITLE", "The report must start with '# Patch Security Analysis'."))
    assessments = re.findall(r"(?m)^- Final Assessment:\s*(\S+)\s*$", stripped)
    assessment = assessments[0] if len(assessments) == 1 else None
    if len(assessments) != 1:
        issues.append(ValidationIssue("ASSESSMENT_COUNT", "Exactly one Final Assessment field is required."))
    elif assessment not in ASSESSMENTS:
        issues.append(ValidationIssue("INVALID_ASSESSMENT", "Final Assessment has an unsupported value."))
    confidences = re.findall(r"(?m)^- Confidence:\s*(\S+)\s*$", stripped)
    confidence = confidences[0] if len(confidences) == 1 else None
    if len(confidences) != 1:
        issues.append(ValidationIssue("CONFIDENCE_COUNT", "Exactly one Confidence field is required."))
    elif confidence not in CONFIDENCE_VALUES:
        issues.append(ValidationIssue("INVALID_CONFIDENCE", "Confidence must be HIGH, MEDIUM, or LOW."))
    sections = _second_level_sections(stripped)
    if tuple(name for name, _ in sections) != EXPECTED_SECTIONS:
        issues.append(
            ValidationIssue(
                "INVALID_SECTIONS",
                "The only level-two sections must be Patch Summary, New Security Issues, and Limitations, in order.",
            )
        )
    section_map = dict(sections)
    if not section_map.get("Patch Summary", "").strip():
        issues.append(ValidationIssue("EMPTY_SUMMARY", "Patch Summary must not be empty."))
    if not section_map.get("Limitations", "").strip():
        issues.append(ValidationIssue("EMPTY_LIMITATIONS", "Limitations must not be empty."))
    findings = section_map.get("New Security Issues", "").strip()
    if assessment == "NO_NEW_ISSUES":
        if findings != "None.":
            issues.append(
                ValidationIssue("NO_ISSUES_CONTENT", "New Security Issues must contain exactly 'None.' for NO_NEW_ISSUES.")
            )
    elif assessment == "NEW_ISSUES_FOUND":
        blocks = _numbered_finding_blocks(findings)
        if not blocks:
            issues.append(ValidationIssue("MISSING_FINDINGS", "NEW_ISSUES_FOUND requires numbered findings."))
        for number, block in blocks:
            for field in ("Location", "Description", "Impact"):
                match = re.search(rf"(?m)^\s{{3}}- {field}:\s*(.+?)\s*$", block)
                if match is None or not match.group(1).strip():
                    issues.append(
                        ValidationIssue("MISSING_FINDING_FIELD", f"Finding {number} is missing a non-empty {field} field.")
                    )
    return ValidationResult(not issues, assessment, confidence, tuple(issues))


def build_contract_repair_prompt(result: ValidationResult) -> str:
    problems = "\n".join(f"- {issue.code}: {issue.message}" for issue in result.issues)
    return (
        "Your previous response did not satisfy the required final-response contract.\n"
        "Return the complete corrected report only. Preserve your evidence-based conclusion; do not include "
        "an acknowledgement, plan, tool transcript, or commentary.\n\n"
        "Validation errors:\n"
        f"{problems}\n\n"
        "Required top-level structure:\n"
        "# Patch Security Analysis\n\n"
        "- Final Assessment: NO_NEW_ISSUES | NEW_ISSUES_FOUND\n"
        "- Confidence: HIGH | MEDIUM | LOW\n\n"
        "## Patch Summary\n\n"
        "## New Security Issues\n\n"
        "## Limitations\n"
    )


def _second_level_sections(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"(?m)^## ([^\n]+)\s*$", text))
    return [
        (
            match.group(1).strip(),
            text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip(),
        )
        for index, match in enumerate(matches)
    ]


def _numbered_finding_blocks(text: str) -> list[tuple[int, str]]:
    matches = list(re.finditer(r"(?m)^(\d+)\.\s+\*\*[^\n]+\*\*\s*$", text))
    return [
        (
            int(match.group(1)),
            text[match.start() : matches[index + 1].start() if index + 1 < len(matches) else len(text)],
        )
        for index, match in enumerate(matches)
    ]


