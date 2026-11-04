"""Strict control-output parsing and permissive analysis StageResult parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

OPTIONAL_STAGE_IDS = (
    "resource_management",
    "locking_synchronization",
    "security_audit",
    "hardware_driver_review",
)


@dataclass(frozen=True)
class PlannedStage:
    stage_id: str
    reason: str


@dataclass(frozen=True)
class StagePlan:
    stages: tuple[PlannedStage, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageResult:
    stage_id: str
    stage_conclusion: str
    concerns: tuple[Any, ...]
    dismissed_concerns: tuple[Any, ...]
    material_limitations: tuple[Any, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "stage_conclusion": self.stage_conclusion,
            "concerns": list(self.concerns),
            "dismissed_concerns": list(self.dismissed_concerns),
            "material_limitations": list(self.material_limitations),
        }


def parse_selection(response: str, *, allow_wrapped: bool = False) -> tuple[str, ...]:
    value = _strict_object(response, expected_keys={"selected_prompts"}, allow_wrapped=allow_wrapped)
    selected = value["selected_prompts"]
    if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
        raise ValueError("selected_prompts must be an array of strings")
    return tuple(selected)


def parse_stage_plan(response: str, *, allow_wrapped: bool = False) -> StagePlan:
    value = _strict_object(response, expected_keys={"conditional_stages"}, allow_wrapped=allow_wrapped)
    items = value["conditional_stages"]
    if not isinstance(items, list):
        raise ValueError("conditional_stages must be an array")
    first: dict[str, PlannedStage] = {}
    warnings: list[str] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"stage_id", "reason"}:
            raise ValueError("each conditional stage must contain exactly stage_id and reason")
        stage_id = item["stage_id"]
        reason = item["reason"]
        if stage_id not in OPTIONAL_STAGE_IDS:
            raise ValueError(f"unknown conditional stage: {stage_id!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"stage reason must be a non-empty string: {stage_id}")
        if stage_id in first:
            warnings.append(f"duplicate stage ignored: {stage_id}")
            continue
        first[stage_id] = PlannedStage(stage_id, reason.strip())
    return StagePlan(tuple(first[name] for name in OPTIONAL_STAGE_IDS if name in first), tuple(warnings))


def parse_stage_result(response: str, *, stage_id: str) -> StageResult:
    values: list[Any] = []
    cleaned = clean_json_control_characters(response.strip())
    for candidate in (cleaned, response.strip()):
        try:
            values.append(json.loads(candidate))
        except (json.JSONDecodeError, TypeError):
            pass
    for candidate in balanced_json_objects(response):
        try:
            values.append(json.loads(clean_json_control_characters(candidate)))
        except (json.JSONDecodeError, TypeError):
            try:
                values.append(json.loads(candidate))
            except (json.JSONDecodeError, TypeError):
                pass
    for value in reversed(values):
        normalized = _normalize_stage_result(value, stage_id)
        if normalized is not None:
            return normalized
    raise ValueError(f"no valid StageResult JSON object found for {stage_id}")


def clean_json_control_characters(value: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    escapes = {"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for char in value:
        if in_string and ord(char) < 0x20:
            output.append(escapes.get(char, f"\\u{ord(char):04x}"))
            escaped = False
            continue
        output.append(char)
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
    return "".join(output)


def remove_trailing_commas(value: str) -> str:
    """Remove commas immediately before a closing JSON delimiter outside strings."""

    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(value) and value[lookahead].isspace():
                lookahead += 1
            if lookahead < len(value) and value[lookahead] in "]}":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def balanced_json_objects(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(value[start : index + 1])
                start = None
    return tuple(candidates)


def _strict_object(response: str, *, expected_keys: set[str], allow_wrapped: bool = False) -> dict[str, Any]:
    candidate = response.strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        if not allow_wrapped:
            raise ValueError("response must be exactly one JSON object") from exc
    else:
        if isinstance(value, dict) and set(value) == expected_keys:
            return value
        if not allow_wrapped:
            raise ValueError(f"response must contain exactly: {', '.join(sorted(expected_keys))}")

    matches: list[dict[str, Any]] = []
    for object_text in balanced_json_objects(candidate):
        variants = (
            object_text,
            clean_json_control_characters(object_text),
            remove_trailing_commas(object_text),
            remove_trailing_commas(clean_json_control_characters(object_text)),
        )
        for variant in variants:
            try:
                value = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and set(value) == expected_keys:
                matches.append(value)
                break
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("response must contain exactly one matching JSON object")
    if not allow_wrapped:
        raise ValueError(f"response must contain exactly: {', '.join(sorted(expected_keys))}")
    raise ValueError(f"response has no JSON object containing exactly: {', '.join(sorted(expected_keys))}")


def _normalize_stage_result(value: Any, stage_id: str) -> StageResult | None:
    if not isinstance(value, dict):
        return None
    arrays: dict[str, tuple[Any, ...]] = {}
    for name in ("concerns", "dismissed_concerns", "material_limitations"):
        raw = value.get(name, [])
        if not isinstance(raw, list):
            return None
        arrays[name] = tuple(raw)
    conclusion = value.get("stage_conclusion", "")
    return StageResult(
        stage_id=stage_id,
        stage_conclusion=conclusion if isinstance(conclusion, str) else "",
        concerns=arrays["concerns"],
        dismissed_concerns=arrays["dismissed_concerns"],
        material_limitations=arrays["material_limitations"],
    )
