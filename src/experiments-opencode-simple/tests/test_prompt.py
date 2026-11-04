from __future__ import annotations

from pathlib import Path

import pytest

from opencode_simple_agent.case_io import CaseData
from opencode_simple_agent.config import DEFAULT_PROMPT_TEMPLATE, EXPECTED_PROMPT_PEER
from opencode_simple_agent.prompt import load_prompt_template, render_prompt


def test_prompt_template_is_byte_identical_to_experiments_codex_simple() -> None:
    assert DEFAULT_PROMPT_TEMPLATE.read_bytes() == EXPECTED_PROMPT_PEER.read_bytes()


def test_render_replaces_only_required_placeholders_with_unicode(tmp_path: Path) -> None:
    case = CaseData("case_0001", tmp_path, tmp_path / "source", "提交信息：修复\n", "diff 内容\n")
    rendered = render_prompt("A={{COMMIT_MESSAGE}}|B={{DIFF}}|C={{SOURCE_TREE}}", case)
    assert rendered == "A=提交信息：修复|B=diff 内容|C=."


def test_missing_or_unresolved_placeholder_is_rejected(tmp_path: Path) -> None:
    template = tmp_path / "prompt.md"
    template.write_text("{{COMMIT_MESSAGE}} {{DIFF}}", encoding="utf-8")
    with pytest.raises(ValueError, match="SOURCE_TREE"):
        load_prompt_template(template)
    case = CaseData("case_0001", tmp_path, tmp_path, "{{UNKNOWN}}", "diff")
    with pytest.raises(ValueError, match="unresolved"):
        render_prompt("{{COMMIT_MESSAGE}} {{DIFF}} {{SOURCE_TREE}}", case)
