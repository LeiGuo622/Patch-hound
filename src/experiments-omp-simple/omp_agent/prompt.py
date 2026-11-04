"""Byte-stable prompt loading and rendering."""

from __future__ import annotations

from pathlib import Path

from .case_io import CaseData

REQUIRED_PLACEHOLDERS = ("COMMIT_MESSAGE", "DIFF", "SOURCE_TREE")


def load_prompt_template(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    template = resolved.read_text(encoding="utf-8")
    missing = [name for name in REQUIRED_PLACEHOLDERS if f"{{{{{name}}}}}" not in template]
    if missing:
        raise ValueError(f"prompt template is missing placeholders: {', '.join(missing)}")
    return template


def render_prompt(template: str, case: CaseData, *, source_tree: str = ".") -> str:
    rendered = template
    replacements = {
        "COMMIT_MESSAGE": case.commit_message.rstrip(),
        "DIFF": case.diff.rstrip(),
        "SOURCE_TREE": source_tree,
    }
    for name, value in replacements.items():
        rendered = rendered.replace(f"{{{{{name}}}}}", value)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("unresolved prompt template placeholder")
    return rendered


def load_case_prompt(case: CaseData, prompt_template: Path) -> str:
    return render_prompt(load_prompt_template(prompt_template), case)


