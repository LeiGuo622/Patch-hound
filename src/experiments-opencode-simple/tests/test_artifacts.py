from __future__ import annotations

import json
from pathlib import Path

from opencode_simple_agent.artifacts import RunArtifacts, bounded_text
from opencode_simple_agent.case_io import allocate_run_paths


def test_atomic_artifacts_and_null_token_fields(tmp_path: Path) -> None:
    paths = allocate_run_paths(tmp_path, output_root=tmp_path / "runs")
    artifacts = RunArtifacts(paths, save_conversation=True)
    artifacts.initialize_manifest({"status": "running"})
    artifacts.update_manifest(status="completed")
    artifacts.record_tokens("initial", {"input": 3, "raw": {"input": 3}})
    artifacts.record_conversation("initial", "prompt", "answer", ())
    artifacts.write_logs()
    assert json.loads(paths.manifest.read_text(encoding="utf-8"))["status"] == "completed"
    usage = paths.token_usage.read_text(encoding="utf-8")
    assert "| initial | 3 | null | null | null | null | null |" in usage
    assert paths.conversation.is_file()
    assert not list(paths.run_dir.glob(".*.tmp-*"))


def test_bounded_text_preserves_digest_and_utf8_boundary() -> None:
    value = "安全" * 20
    bounded = bounded_text(value, 7)
    assert bounded.truncated
    assert bounded.raw_bytes == len(value.encode())
    assert bounded.stored_bytes == 7
    bounded.text.encode("utf-8")
    assert len(bounded.sha256) == 64
