import pytest

from omp_expert_agent.stage_output import parse_selection, parse_stage_result


def test_control_compatibility_accepts_one_matching_wrapped_object():
    response = 'Analysis first.\n```json\n{"selected_prompts":["net.md"]}\n```'
    assert parse_selection(response, allow_wrapped=True) == ("net.md",)
    with pytest.raises(ValueError, match="exactly one JSON object"):
        parse_selection(response)
    with pytest.raises(ValueError):
        parse_selection(
            '{"selected_prompts":[]} then {"selected_prompts":["net.md"]}',
            allow_wrapped=True,
        )
    assert parse_selection(
        'Example: {"ignored":true}\nResult: {"selected_prompts":["net.md",],}',
        allow_wrapped=True,
    ) == ("net.md",)


def test_stage_result_accepts_trailing_commas_from_model_json():
    result = parse_stage_result(
        '{"stage_conclusion":"ok","concerns":[],"dismissed_concerns":[{"evidence":[{"observation":"comma, } stays in string",},],},],}',
        stage_id="execution_flow",
    )
    assert result.stage_id == "execution_flow"
    assert result.dismissed_concerns[0]["evidence"][0]["observation"] == "comma, } stays in string"
