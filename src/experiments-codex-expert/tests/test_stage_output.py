from __future__ import annotations

import sys
import unittest
from pathlib import Path

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from expert_agent.stage_output import parse_selection, parse_stage_plan, parse_stage_result  # noqa: E402


class StageOutputTest(unittest.TestCase):
    def test_selection_is_strict_whole_response_json(self) -> None:
        self.assertEqual(parse_selection('{"selected_prompts":["net.md"]}'), ("net.md",))
        for response in (
            'text {"selected_prompts":[]}',
            '{"selected_prompts":[],"extra":1}',
            '{"selected_prompts":[1]}',
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                parse_selection(response)
        self.assertEqual(parse_selection('{"selected_prompts":["a.md","a.md"]}'), ("a.md", "a.md"))

    def test_control_compatibility_accepts_one_matching_wrapped_object(self) -> None:
        response = 'Analysis first.\n```json\n{"selected_prompts":["net.md"]}\n```'
        self.assertEqual(parse_selection(response, allow_wrapped=True), ("net.md",))
        with self.assertRaisesRegex(ValueError, "exactly one JSON object"):
            parse_selection(response)
        with self.assertRaises(ValueError):
            parse_selection(
                '{"selected_prompts":[]} then {"selected_prompts":["net.md"]}',
                allow_wrapped=True,
            )
        self.assertEqual(
            parse_selection(
                'Example: {"ignored":true}\nResult: {"selected_prompts":["net.md",],}',
                allow_wrapped=True,
            ),
            ("net.md",),
        )

    def test_planner_normalizes_order_and_keeps_first_duplicate(self) -> None:
        plan = parse_stage_plan(
            '{"conditional_stages":['
            '{"stage_id":"security_audit","reason":"security"},'
            '{"stage_id":"resource_management","reason":"resource"},'
            '{"stage_id":"security_audit","reason":"duplicate"}]}'
        )
        self.assertEqual([item.stage_id for item in plan.stages], ["resource_management", "security_audit"])
        self.assertEqual(plan.warnings, ("duplicate stage ignored: security_audit",))

    def test_planner_rejects_unknown_missing_or_empty_reason(self) -> None:
        for response in (
            '{"conditional_stages":[{"stage_id":"unknown","reason":"x"}]}',
            '{"conditional_stages":[{"stage_id":"security_audit"}]}',
            '{"conditional_stages":[{"stage_id":"security_audit","reason":""}]}',
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                parse_stage_plan(response)

    def test_stage_result_accepts_empty_and_overrides_stage_id(self) -> None:
        self.assertEqual(parse_stage_result("{}", stage_id="security_audit").as_dict(), {
            "stage_id": "security_audit", "stage_conclusion": "", "concerns": [], "dismissed_concerns": [], "material_limitations": []
        })
        result = parse_stage_result(
            'prefix {"stage_id":"wrong","stage_conclusion":1,"concerns":[]} suffix',
            stage_id="execution_flow",
        )
        self.assertEqual(result.stage_id, "execution_flow")
        self.assertEqual(result.stage_conclusion, "")

    def test_stage_result_uses_last_valid_object_and_cleans_controls(self) -> None:
        response = '{"concerns":"bad"}\ntext\n{"stage_conclusion":"line\nvalue","concerns":[{"x":1}]}'
        result = parse_stage_result(response, stage_id="security_audit")
        self.assertEqual(result.stage_conclusion, "line\nvalue")
        self.assertEqual(result.concerns, ({"x": 1},))

    def test_stage_result_extracts_last_object_from_markdown_fence(self) -> None:
        response = '```json\n{"stage_conclusion":"first"}\n```\n```json\n{"stage_conclusion":"last"}\n```'
        self.assertEqual(parse_stage_result(response, stage_id="execution_flow").stage_conclusion, "last")

    def test_stage_result_rejects_non_array_contract_fields(self) -> None:
        with self.assertRaises(ValueError):
            parse_stage_result('{"concerns":{}}', stage_id="security_audit")


if __name__ == "__main__":
    unittest.main()
