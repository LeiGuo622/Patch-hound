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

from expert_agent.events import TurnExecution  # noqa: E402
from expert_agent.guidance import load_corpus  # noqa: E402
from expert_agent.workflow import (  # noqa: E402
    CallResult,
    load_prompts,
    normalize_manual_stages,
    render_analysis_prompt,
    render_final_prompt,
    run_case,
)


def prepare_case(root: Path) -> tuple[Path, Path]:
    cases = root / "cases"
    case = cases / "case_0001"
    (case / "input").mkdir(parents=True)
    (case / "source").mkdir()
    (case / "input" / "commit_message.txt").write_text("test commit\n", encoding="utf-8")
    (case / "input" / "diff.txt").write_text("diff --git a/a.c b/a.c\n+{{DATA_BRACES}}\n", encoding="utf-8")
    (case / "source" / "a.c").write_text("int a;\n", encoding="utf-8")
    guidance = root / "guidance"
    subsystem = guidance / "subsystem"
    subsystem.mkdir(parents=True)
    (subsystem / "subsystem.md").write_text(
        "| Subsystem | Triggers | File |\n|---|---|---|\n| Net | a.c | net.md |\n| Lock | lock | locking.md |\n",
        encoding="utf-8",
    )
    (subsystem / "net.md").write_text("NET_GUIDANCE\n", encoding="utf-8")
    (subsystem / "locking.md").write_text("LOCK_GUIDANCE\n", encoding="utf-8")
    (guidance / "callstack.md").write_text("CALLSTACK_GUIDANCE\n", encoding="utf-8")
    (guidance / "technical-patterns.md").write_text("PATTERN_GUIDANCE\n", encoding="utf-8")
    (guidance / "false-positive-guide.md").write_text("FP_GUIDANCE\n", encoding="utf-8")
    return cases, guidance


def result(name: str, response: str, number: int) -> CallResult:
    sdk_result = SimpleNamespace(final_response=response, id=f"turn-{number}", usage=None, items=())
    execution = TurnExecution(sdk_result, ())
    return CallResult(name, response, execution, f"thread-{number}")


class WorkflowTest(unittest.TestCase):
    def test_serial_calls_transfer_only_explicit_outputs_and_generate_baseline_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases, guidance = prepare_case(root)
            calls: list[tuple[str, str, Path]] = []
            responses = {
                "knowledge_selection": '{"selected_prompts":["net.md","locking.md"]}',
                "stage_planning": (
                    '{"conditional_stages":[{"stage_id":"security_audit",'
                    '"reason":"PRIVATE_PLANNING_REASON"}]}'
                ),
                "execution_flow": '{}',
                "security_audit": (
                    '{"stage_conclusion":"candidate","concerns":[{"summary":"issue"}],'
                    '"dismissed_concerns":[],"material_limitations":[]}'
                ),
                "final_synthesis": "arbitrary nonempty final response\n",
            }

            def fake_invoke(**kwargs: object) -> CallResult:
                name = str(kwargs["name"])
                calls.append((name, str(kwargs["prompt"]), Path(kwargs["call_root"])))
                return result(name, responses[name], len(calls))

            with patch("expert_agent.workflow._invoke_call", side_effect=fake_invoke):
                completed = run_case(
                    "case_0001",
                    cases_dir=cases,
                    runtime_config=EXPERIMENT_ROOT / "codex_runtime.toml",
                    guidance_root=guidance,
                    phoenix_enabled=False,
                    use_history_tool=False,
                    live_output=False,
                )

            self.assertEqual([name for name, _prompt, _root in calls], [
                "knowledge_selection", "stage_planning", "execution_flow", "security_audit", "final_synthesis"
            ])
            self.assertEqual(len({root_path for _name, _prompt, root_path in calls}), len(calls))
            execution_prompt = next(prompt for name, prompt, _root in calls if name == "execution_flow")
            security_prompt = next(prompt for name, prompt, _root in calls if name == "security_audit")
            final_prompt = next(prompt for name, prompt, _root in calls if name == "final_synthesis")
            self.assertNotIn("PRIVATE_PLANNING_REASON", execution_prompt)
            self.assertNotIn("PRIVATE_PLANNING_REASON", security_prompt)
            self.assertNotIn('"summary": "issue"', execution_prompt)
            self.assertIn('"summary": "issue"', final_prompt)
            self.assertIn("NET_GUIDANCE", execution_prompt)
            self.assertNotIn("LOCK_GUIDANCE", execution_prompt)
            self.assertIn("FP_GUIDANCE", final_prompt)
            self.assertEqual(completed.final_response, responses["final_synthesis"])
            self.assertEqual(
                {path.name for path in completed.run_dir.iterdir()},
                {"analysis.md", "run_manifest.json", "model_config.md", "process_log.md", "token_usage.md"},
            )
            manifest = json.loads((completed.run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stage_plan"], ["execution_flow", "security_audit"])
            self.assertEqual(set(manifest["thread_ids"]), set(responses))
            self.assertIsNone(manifest["final_assessment"])

    def test_planner_failure_runs_all_optional_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases, guidance = prepare_case(root)
            called: list[str] = []

            def fake_invoke(**kwargs: object) -> CallResult:
                name = str(kwargs["name"])
                called.append(name)
                if name == "knowledge_selection":
                    response = '{"selected_prompts":[]}'
                elif name == "stage_planning":
                    response = "not-json"
                elif name == "final_synthesis":
                    response = "# final"
                    self.assertIn("Stage planning failed", str(kwargs["prompt"]))
                else:
                    response = "{}"
                return result(name, response, len(called))

            with patch("expert_agent.workflow._invoke_call", side_effect=fake_invoke):
                completed = run_case(
                    "case_0001",
                    cases_dir=cases,
                    runtime_config=EXPERIMENT_ROOT / "codex_runtime.toml",
                    guidance_root=guidance,
                    phoenix_enabled=False,
                    use_history_tool=False,
                    live_output=False,
                )
            self.assertEqual(called[2:-1], [
                "execution_flow", "resource_management", "locking_synchronization", "security_audit", "hardware_driver_review"
            ])
            manifest = json.loads((completed.run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["workflow_limitations"])

    def test_manual_stage_order_and_prompt_composition(self) -> None:
        self.assertEqual(normalize_manual_stages(["security_audit", "resource_management", "security_audit"]), (
            "resource_management", "security_audit"
        ))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases, guidance = prepare_case(root)
            corpus = load_corpus(guidance)
            prompts = load_prompts()
            case = SimpleNamespace(commit_message="message", diff="diff")
            stage_prompt = render_analysis_prompt(prompts, corpus, case, "execution_flow", corpus.documents_for("execution_flow", ()))
            final_prompt = render_final_prompt(prompts, corpus, case, (), (), ())
            self.assertLess(stage_prompt.index("PatchHound staged"), stage_prompt.index("Focused analysis stage"))
            self.assertLess(final_prompt.index("PatchHound staged"), final_prompt.index("PatchHound final"))

    def test_selector_failure_is_fatal_and_final_empty_does_not_write_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases, guidance = prepare_case(root)
            calls: list[str] = []

            def bad_selector(**kwargs: object) -> CallResult:
                name = str(kwargs["name"])
                calls.append(name)
                return result(name, "not-json" if name == "knowledge_selection" else "", len(calls))

            with patch("expert_agent.workflow._invoke_call", side_effect=bad_selector):
                with self.assertRaises(ValueError):
                    run_case(
                        "case_0001",
                        cases_dir=cases,
                        runtime_config=EXPERIMENT_ROOT / "codex_runtime.toml",
                        guidance_root=guidance,
                        phoenix_enabled=False,
                        use_history_tool=False,
                        live_output=False,
                    )
            run_dirs = list((cases / "case_0001" / "expert-runs").iterdir())
            self.assertEqual(len(run_dirs), 1)
            self.assertNotIn("analysis.md", {path.name for path in run_dirs[0].iterdir()})

            calls.clear()

            def empty_final(**kwargs: object) -> CallResult:
                name = str(kwargs["name"])
                calls.append(name)
                if name == "knowledge_selection":
                    response = '{"selected_prompts":[]}'
                elif name == "stage_planning":
                    response = '{"conditional_stages":[]}'
                elif name == "execution_flow":
                    response = "{}"
                else:
                    response = ""
                return result(name, response, len(calls))

            with patch("expert_agent.workflow._invoke_call", side_effect=empty_final):
                with self.assertRaises(RuntimeError):
                    run_case(
                        "case_0001",
                        cases_dir=cases,
                        runtime_config=EXPERIMENT_ROOT / "codex_runtime.toml",
                        guidance_root=guidance,
                        phoenix_enabled=False,
                        use_history_tool=False,
                        live_output=False,
                    )
            run_dirs = sorted((cases / "case_0001" / "expert-runs").iterdir())
            self.assertNotIn("analysis.md", {path.name for path in run_dirs[-1].iterdir()})

    def test_failed_analysis_is_carried_as_final_limitation_and_later_stage_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases, guidance = prepare_case(root)
            calls: list[str] = []

            def one_failed_stage(**kwargs: object) -> CallResult:
                name = str(kwargs["name"])
                calls.append(name)
                if name == "knowledge_selection":
                    response = '{"selected_prompts":[]}'
                elif name == "stage_planning":
                    response = '{"conditional_stages":[{"stage_id":"security_audit","reason":"r"},{"stage_id":"hardware_driver_review","reason":"h"}]}'
                elif name == "security_audit":
                    response = "invalid stage output"
                elif name == "final_synthesis":
                    response = "# final"
                    self.assertIn("Analysis stage security_audit failed", str(kwargs["prompt"]))
                else:
                    response = "{}"
                return result(name, response, len(calls))

            with patch("expert_agent.workflow._invoke_call", side_effect=one_failed_stage):
                completed = run_case(
                    "case_0001",
                    cases_dir=cases,
                    runtime_config=EXPERIMENT_ROOT / "codex_runtime.toml",
                    guidance_root=guidance,
                    phoenix_enabled=False,
                    use_history_tool=False,
                    live_output=False,
                )
            self.assertEqual(calls, ["knowledge_selection", "stage_planning", "execution_flow", "security_audit", "hardware_driver_review", "final_synthesis"])
            manifest = json.loads((completed.run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stages"]["security_audit"]["status"], "failed")
            self.assertTrue(any("security_audit" in item for item in manifest["workflow_limitations"]))


if __name__ == "__main__":
    unittest.main()
