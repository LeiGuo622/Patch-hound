from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EXPERIMENT_ROOT.parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from simple_agent.case_io import allocate_run_paths, load_case  # noqa: E402
from simple_agent.config import DEFAULT_PROMPT_TEMPLATE  # noqa: E402
from simple_agent.prompt import load_case_prompt, load_prompt_template  # noqa: E402


def make_case(root: Path, case_id: str = "case_0001") -> Path:
    case = root / "cases" / case_id
    (case / "input").mkdir(parents=True)
    (case / "source").mkdir()
    (case / "input" / "commit_message.txt").write_text("subject\nbody\n", encoding="utf-8")
    (case / "input" / "diff.txt").write_text("@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")
    return case


class PromptTest(unittest.TestCase):
    def test_prompt_was_moved_and_simplified(self) -> None:
        old = PROJECT_ROOT / "src" / "experiments" / "prompts" / "single_task_patch_verification.md"
        text = DEFAULT_PROMPT_TEMPLATE.read_text(encoding="utf-8")

        self.assertFalse(old.exists())
        self.assertIn("# PatchHound patch security verification", text)
        self.assertNotIn("single-task", text.lower())
        self.assertNotIn("one continuous task", text.lower())
        self.assertIn("analysis_stage`: use `patch security review`", text)
        self.assertNotIn("Reachability and security context", text)
        self.assertNotIn("Security reasoning", text)
        self.assertNotIn("Rejected concerns", text)
        self.assertNotIn("For each security-relevant changed function", text)

    def test_prompt_has_expected_structure_and_placeholders(self) -> None:
        text = load_prompt_template(DEFAULT_PROMPT_TEMPLATE)
        for heading in (
            "## Role and objective",
            "## Case background",
            "## Tool use",
            "### Source-tree operations and evidence boundary",
            "### Controlled pre-patch history",
            "## Required investigation",
            "## Finding acceptance standard",
            "## Final response contract",
            "## Input",
        ):
            self.assertIn(heading, text)
        for placeholder in ("{{COMMIT_MESSAGE}}", "{{DIFF}}", "{{SOURCE_TREE}}"):
            self.assertIn(placeholder, text)

    def test_case_prompt_embeds_inputs_and_hides_case_input_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_case(root)
            case = load_case("case_0001", root / "cases")
            prompt = load_case_prompt(case, DEFAULT_PROMPT_TEMPLATE)

        self.assertIn("subject\nbody", prompt)
        self.assertIn("@@ -1 +1 @@", prompt)
        self.assertIn("<source_tree>\n.\n</source_tree>", prompt)
        self.assertNotIn(str(case.case_dir / "input"), prompt)
        self.assertNotIn("{{", prompt)

    def test_case_rejects_git_and_run_namespace_is_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = make_case(root)
            first = allocate_run_paths(case)
            second = allocate_run_paths(case)
            (case / "runs" / "run_9999").mkdir(parents=True)
            third = allocate_run_paths(case)
            (case / "source" / ".git").mkdir()
            with self.assertRaisesRegex(RuntimeError, "must not contain"):
                load_case("case_0001", root / "cases")

        self.assertEqual(first.run_id, "run_0001")
        self.assertEqual(second.run_id, "run_0002")
        self.assertEqual(third.run_id, "run_0003")
        self.assertEqual(first.run_dir.parent.name, "simple-runs")

    def test_case_validation_rejects_bad_id_and_missing_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = make_case(root)
            (case / "input" / "diff.txt").unlink()
            with self.assertRaisesRegex(ValueError, "case_id"):
                load_case("../case_0001", root / "cases")
            with self.assertRaises(FileNotFoundError):
                load_case("case_0001", root / "cases")

    def test_force_run_requires_and_only_replaces_explicit_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case = make_case(Path(temporary))
            with self.assertRaisesRegex(ValueError, "requires --run-id"):
                allocate_run_paths(case, force_run=True)

            target = allocate_run_paths(case, run_id="run_0007")
            marker = target.run_dir / "marker"
            marker.write_text("old", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                allocate_run_paths(case, run_id="run_0007")

            replacement = allocate_run_paths(case, run_id="run_0007", force_run=True)
            self.assertEqual(replacement.run_dir, target.run_dir)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
