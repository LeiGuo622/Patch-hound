from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from opencode_simple_agent.case_io import load_case
from opencode_simple_agent.config import DEFAULT_PROMPT_TEMPLATE, EXPECTED_PROMPT_PEER
from opencode_simple_agent.prompt import load_case_prompt

SIMPLE_ROOT = Path(__file__).resolve().parents[2] / "experiments-codex-simple"
sys.path.insert(0, str(SIMPLE_ROOT))
from simple_agent.prompt import load_case_prompt as baseline_prompt  # noqa: E402


def test_rendered_prompt_matches_experiments_codex_simple(prepared_cases: Path) -> None:
    case = load_case("case_0001", prepared_cases)
    ours = load_case_prompt(case, DEFAULT_PROMPT_TEMPLATE)
    baseline = baseline_prompt(case, EXPECTED_PROMPT_PEER)
    assert ours == baseline
    assert "修复 carrier race" in ours


def test_runtime_contract_matches_peers_except_sdk_selection() -> None:
    """Guard the comparison variables while allowing backend-native names."""
    simple = tomllib.loads((SIMPLE_ROOT / "codex_runtime.toml").read_text(encoding="utf-8"))
    omp = tomllib.loads(
        (SIMPLE_ROOT.parent / "experiments-omp-simple" / "omp_runtime.toml").read_text(encoding="utf-8")
    )
    opencode = tomllib.loads((Path(__file__).parents[1] / "opencode_runtime.toml").read_text(encoding="utf-8"))

    simple_experiment = {
        "model": simple["model"],
        "reasoning_effort": simple["model_reasoning_effort"],
        "network_enabled": simple["permissions"]["patch_review"]["network"]["enabled"],
        "source_access": simple["permissions"]["patch_review"]["filesystem"][":workspace_roots"]["."],
        "history_persistence": simple["history"]["persistence"],
        "memories": simple["features"]["memories"],
    }
    for peer in (omp["experiment"], opencode["experiment"]):
        assert peer["logical_model"] == simple_experiment["model"]
        assert peer["reasoning_effort"] == simple_experiment["reasoning_effort"]
        assert peer["network_enabled"] is simple_experiment["network_enabled"]
        assert peer["timeout_seconds"] == 7200
        assert peer["max_contract_repairs"] == 2
        assert peer["history_mcp_required"] is True
    assert simple_experiment["source_access"] == "read"
    assert simple_experiment["history_persistence"] == "none"
    assert simple_experiment["memories"] is False

    assert omp["experiment"]["provider"] == "openai-codex"  # backend selector
    assert opencode["experiment"]["provider"] == "openai"  # backend selector
    assert omp["limits"] == opencode["limits"]
    assert omp["runtime"]["sidecar_protocol_version"] == opencode["runtime"]["sidecar_protocol_version"] == 1
    assert omp["runtime"]["server_ready_timeout_seconds"] == opencode["runtime"]["server_ready_timeout_seconds"] == 15
    assert omp["runtime"]["mcp_timeout_ms"] == opencode["runtime"]["mcp_timeout_ms"] == 30_000
    assert omp["runtime"]["dispose_grace_seconds"] == opencode["runtime"]["dispose_grace_seconds"] == 5
    assert omp["runtime"]["auth_required"] is opencode["runtime"]["auth_required"] is True

    normalized_omp_tools = {
        "history" if value == "query_pre_patch_history" else value for value in omp["tools"]["enabled"]
    }
    normalized_opencode_tools = {
        "history"
        if value == "patchhound_history_query_pre_patch_history"
        else value
        for value in opencode["tools"]["enabled"]
    }
    assert normalized_omp_tools == normalized_opencode_tools == {"bash", "read", "glob", "grep", "history"}
