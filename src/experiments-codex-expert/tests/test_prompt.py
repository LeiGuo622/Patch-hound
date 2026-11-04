from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from expert_agent.prompt import load_template, render_template  # noqa: E402
from expert_agent.workflow import load_prompts  # noqa: E402


class PromptTest(unittest.TestCase):
    def test_all_repository_prompts_validate(self) -> None:
        prompts = load_prompts()
        self.assertEqual(set(prompts.stage_tasks), {
            "execution_flow", "resource_management", "locking_synchronization", "security_audit", "hardware_driver_review"
        })

    def test_render_is_one_pass_and_preserves_inserted_braces(self) -> None:
        rendered = render_template("before {{DIFF}} after", {"DIFF": "x {{NOT_A_TEMPLATE}} y"})
        self.assertEqual(rendered, "before x {{NOT_A_TEMPLATE}} y after")

    def test_loader_rejects_missing_and_unknown_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prompt.md"
            path.write_text("{{A}} {{B}}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown"):
                load_template(path, required={"A"})
            with self.assertRaisesRegex(ValueError, "missing"):
                load_template(path, required={"A", "B", "C"})


if __name__ == "__main__":
    unittest.main()
