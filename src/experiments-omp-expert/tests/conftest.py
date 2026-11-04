from __future__ import annotations

import sys
from pathlib import Path

import pytest

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))


VALID_NO_ISSUES = """# Patch Security Analysis

- Final Assessment: NO_NEW_ISSUES
- Confidence: HIGH

## Patch Summary

The patch adds a carrier-state check.

## New Security Issues

None.

## Limitations

Only the prepared source snapshot was reviewed.
"""

VALID_NEW_ISSUE = """# Patch Security Analysis

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

Runtime behavior was not exercised.
"""


@pytest.fixture
def prepared_cases(tmp_path: Path) -> Path:
    cases = tmp_path / "cases"
    case = cases / "case_0001"
    (case / "input").mkdir(parents=True)
    (case / "source" / "drivers").mkdir(parents=True)
    (case / "input" / "commit_message.txt").write_text("修复 carrier race\n", encoding="utf-8")
    (case / "input" / "diff.txt").write_text(
        "diff --git a/drivers/test.c b/drivers/test.c\n"
        "--- a/drivers/test.c\n"
        "+++ b/drivers/test.c\n"
        "@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8",
    )
    (case / "source" / "drivers" / "test.c").write_text("int changed;\n", encoding="utf-8")
    return cases

