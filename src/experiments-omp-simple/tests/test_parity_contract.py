from __future__ import annotations

import sys
from pathlib import Path

from omp_agent.case_io import load_case
from omp_agent.config import DEFAULT_PROMPT_TEMPLATE, EXPECTED_PROMPT_PEER
from omp_agent.prompt import load_case_prompt

SIMPLE_ROOT = Path(__file__).resolve().parents[2] / "experiments-codex-simple"
sys.path.insert(0, str(SIMPLE_ROOT))
from simple_agent.prompt import load_case_prompt as baseline_prompt  # noqa: E402


def test_rendered_prompt_matches_experiments_codex_simple(prepared_cases: Path) -> None:
    case = load_case("case_0001", prepared_cases)
    ours = load_case_prompt(case, DEFAULT_PROMPT_TEMPLATE)
    baseline = baseline_prompt(case, EXPECTED_PROMPT_PEER)
    assert ours == baseline
    assert "修复 carrier race" in ours

