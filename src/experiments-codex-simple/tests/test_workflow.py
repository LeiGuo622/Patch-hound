from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from simple_agent.artifacts import RunArtifacts  # noqa: E402
from simple_agent.case_io import allocate_run_paths  # noqa: E402
from simple_agent.config import DEFAULT_PROMPT_TEMPLATE, DEFAULT_RUNTIME_CONFIG  # noqa: E402
from simple_agent.events import TurnExecution  # noqa: E402
from simple_agent.validation import ValidationResult, validate_final_response  # noqa: E402
from simple_agent.workflow import _execute_codex, run_case  # noqa: E402

VALID_RESPONSE = """# Patch Security Analysis

- Final Assessment: NO_NEW_ISSUES
- Confidence: HIGH

## Patch Summary

The patch changes validation.

## New Security Issues

None.

## Limitations

None.
"""


def make_case(root: Path) -> Path:
    case = root / "cases" / "case_0001"
    (case / "input").mkdir(parents=True)
    (case / "source").mkdir()
    (case / "input" / "commit_message.txt").write_text("subject\n", encoding="utf-8")
    (case / "input" / "diff.txt").write_text("@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")
    return case


class FakeApproval:
    deny_all = "deny_all"


class FakeSandbox:
    workspace_write = "workspace-write"


class FakeThread:
    id = "thread-1"


class FakeCodex:
    starts = 0

    def __init__(self, *, config: object) -> None:
        self.config = config

    def __enter__(self) -> FakeCodex:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def thread_start(self, **_kwargs: object) -> FakeThread:
        type(self).starts += 1
        return FakeThread()


class FakePhoenix:
    trace_id = "phoenix-trace"
    warning_count = 0

    def __init__(self) -> None:
        self.checkpoints: list[str] = []
        self.completed = False

    def record_checkpoint(self, name: str, **_kwargs: object) -> None:
        self.checkpoints.append(name)

    def complete(self, **_kwargs: object) -> None:
        self.completed = True

    def fail(self, **_kwargs: object) -> None:
        pass


def execution(turn_id: str, response: str) -> TurnExecution:
    result = SimpleNamespace(id=turn_id, final_response=response, usage=None)
    return TurnExecution(result=result, events=())


class WorkflowTest(unittest.TestCase):
    def test_run_writes_completed_host_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = make_case(root)
            validation = validate_final_response(VALID_RESPONSE)
            with patch(
                "simple_agent.workflow._execute_codex",
                return_value=(VALID_RESPONSE, validation, "thread-1", ["turn-1"]),
            ):
                result = run_case(
                    "case_0001",
                    cases_dir=root / "cases",
                    prompt_template=DEFAULT_PROMPT_TEMPLATE,
                    runtime_config=DEFAULT_RUNTIME_CONFIG,
                    phoenix_enabled=False,
                    live_output=False,
                )

            manifest = json.loads((result.run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            names = {path.name for path in result.run_dir.iterdir()}

            self.assertEqual(result.run_dir.parent, case / "simple-runs")
            self.assertEqual(
                names,
                {
                    "analysis.md",
                    "history_queries.md",
                    "run_manifest.json",
                    "model_config.md",
                    "process_log.md",
                    "token_usage.md",
                },
            )
            self.assertIn(
                "No persisted history queries were recorded",
                (result.run_dir / "history_queries.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["final_assessment"], "NO_NEW_ISSUES")
            self.assertEqual(result.output_path.read_text(encoding="utf-8"), VALID_RESPONSE)

    def test_run_records_failure_without_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_case(root)
            with patch("simple_agent.workflow._execute_codex", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    run_case(
                        "case_0001",
                        cases_dir=root / "cases",
                        runtime_config=DEFAULT_RUNTIME_CONFIG,
                        phoenix_enabled=False,
                        live_output=False,
                    )
            run_dir = root / "cases" / "case_0001" / "simple-runs" / "run_0001"
            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest["status"], "failed")
            self.assertFalse((run_dir / "analysis.md").exists())
            self.assertTrue((run_dir / "process_log.md").is_file())

    def test_phoenix_initialization_failure_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_case(root)
            validation = validate_final_response(VALID_RESPONSE)
            with (
                patch(
                    "simple_agent.workflow.PhoenixObserver.create",
                    side_effect=RuntimeError("collector unavailable"),
                ),
                patch(
                    "simple_agent.workflow._execute_codex",
                    return_value=(VALID_RESPONSE, validation, "thread-1", ["turn-1"]),
                ),
            ):
                result = run_case(
                    "case_0001",
                    cases_dir=root / "cases",
                    runtime_config=DEFAULT_RUNTIME_CONFIG,
                    phoenix_enabled=True,
                    live_output=False,
                )

            manifest = json.loads((result.run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            process_log = (result.run_dir / "process_log.md").read_text(encoding="utf-8")
            self.assertEqual(manifest["status"], "completed")
            self.assertTrue(result.output_path.is_file())
            self.assertIn("Phoenix setup disabled", process_log)

    def test_successful_workflow_records_runtime_and_artifact_spans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_case(root)
            validation = validate_final_response(VALID_RESPONSE)
            phoenix = FakePhoenix()
            with (
                patch("simple_agent.workflow.PhoenixObserver.create", return_value=phoenix),
                patch(
                    "simple_agent.workflow._execute_codex",
                    return_value=(VALID_RESPONSE, validation, "thread-1", ["turn-1"]),
                ),
            ):
                result = run_case(
                    "case_0001",
                    cases_dir=root / "cases",
                    runtime_config=DEFAULT_RUNTIME_CONFIG,
                    phoenix_enabled=True,
                    live_output=False,
                )

            self.assertTrue(phoenix.completed)
            self.assertIn("runtime.configuration", phoenix.checkpoints)
            self.assertIn("artifact.analysis", phoenix.checkpoints)
            self.assertIn("artifact.run_manifest", phoenix.checkpoints)
            self.assertIn("artifact.process_log", phoenix.checkpoints)
            self.assertEqual(result.phoenix_trace_id, "phoenix-trace")

    def test_invalid_phoenix_configuration_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_case(root)
            validation = validate_final_response(VALID_RESPONSE)
            with (
                patch(
                    "simple_agent.workflow.PhoenixConfig.load",
                    side_effect=ValueError("invalid telemetry setting"),
                ),
                patch(
                    "simple_agent.workflow._execute_codex",
                    return_value=(VALID_RESPONSE, validation, "thread-1", ["turn-1"]),
                ),
            ):
                result = run_case(
                    "case_0001",
                    cases_dir=root / "cases",
                    runtime_config=DEFAULT_RUNTIME_CONFIG,
                    live_output=False,
                )

            manifest = json.loads((result.run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            process_log = (result.run_dir / "process_log.md").read_text(encoding="utf-8")
            self.assertEqual(manifest["status"], "completed")
            self.assertFalse(manifest["phoenix_enabled"])
            self.assertIn("Phoenix configuration disabled", process_log)

    def test_execute_codex_repairs_contract_in_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            scratch = root / "scratch"
            source.mkdir()
            scratch.mkdir()
            run_paths = allocate_run_paths(root)
            artifacts = RunArtifacts(run_paths)
            prompts: list[str] = []
            responses = iter([execution("turn-1", "invalid"), execution("turn-2", VALID_RESPONSE)])

            def fake_turn(**kwargs: object) -> TurnExecution:
                prompts.append(str(kwargs["prompt"]))
                return next(responses)

            FakeCodex.starts = 0
            isolated = SimpleNamespace(
                codex_home=root / "codex-home",
                sqlite_home=root / "codex-sqlite",
                scratch_dir=scratch,
                env={},
            )
            with (
                patch(
                    "simple_agent.workflow.import_codex_sdk",
                    return_value=(FakeApproval, FakeCodex, FakeSandbox),
                ),
                patch("simple_agent.workflow.prepare_isolated_environment", return_value=isolated),
                patch("simple_agent.workflow.build_codex_config", return_value=SimpleNamespace(codex_bin=None)),
                patch("simple_agent.workflow.build_session_config", return_value={}),
                patch("simple_agent.workflow.run_turn_with_timeout", side_effect=fake_turn),
            ):
                final, validation, thread_id, turn_ids = _execute_codex(
                    case_id="case_0001",
                    source_dir=source,
                    prompt="initial prompt",
                    runtime_config=DEFAULT_RUNTIME_CONFIG,
                    run_paths=run_paths,
                    artifacts=artifacts,
                    phoenix=None,
                    codex_bin=None,
                    model=None,
                    timeout_seconds=60,
                    max_contract_repairs=2,
                    use_history_tool=False,
                    history_linux_repo=root,
                    live_output=False,
                    status=lambda _message: None,
                )

        self.assertEqual(final, VALID_RESPONSE.strip())
        self.assertTrue(validation.ok)
        self.assertEqual(thread_id, "thread-1")
        self.assertEqual(turn_ids, ["turn-1", "turn-2"])
        self.assertEqual(FakeCodex.starts, 1)
        self.assertEqual(prompts[0], "initial prompt")
        self.assertIn("INVALID_TITLE", prompts[1])

    def test_validation_type_is_stable_for_mocked_execution(self) -> None:
        result = validate_final_response(VALID_RESPONSE)
        self.assertIsInstance(result, ValidationResult)

    def test_execute_codex_rejects_repeatedly_invalid_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            scratch = root / "scratch"
            source.mkdir()
            scratch.mkdir()
            run_paths = allocate_run_paths(root)
            artifacts = RunArtifacts(run_paths)
            responses = [execution(f"turn-{index}", "invalid") for index in range(1, 4)]
            FakeCodex.starts = 0
            isolated = SimpleNamespace(
                codex_home=root / "codex-home",
                sqlite_home=root / "codex-sqlite",
                scratch_dir=scratch,
                env={},
            )
            with (
                patch(
                    "simple_agent.workflow.import_codex_sdk",
                    return_value=(FakeApproval, FakeCodex, FakeSandbox),
                ),
                patch("simple_agent.workflow.prepare_isolated_environment", return_value=isolated),
                patch("simple_agent.workflow.build_codex_config", return_value=SimpleNamespace(codex_bin=None)),
                patch("simple_agent.workflow.build_session_config", return_value={}),
                patch("simple_agent.workflow.run_turn_with_timeout", side_effect=responses),
            ):
                with self.assertRaisesRegex(RuntimeError, "failed contract validation"):
                    _execute_codex(
                        case_id="case_0001",
                        source_dir=source,
                        prompt="initial prompt",
                        runtime_config=DEFAULT_RUNTIME_CONFIG,
                        run_paths=run_paths,
                        artifacts=artifacts,
                        phoenix=None,
                        codex_bin=None,
                        model=None,
                        timeout_seconds=60,
                        max_contract_repairs=2,
                        use_history_tool=False,
                        history_linux_repo=root,
                        live_output=False,
                        status=lambda _message: None,
                    )

            self.assertEqual(FakeCodex.starts, 1)
            self.assertFalse(run_paths.analysis.exists())
            self.assertEqual(run_paths.invalid_response.read_text(encoding="utf-8"), "invalid\n")


if __name__ == "__main__":
    unittest.main()
