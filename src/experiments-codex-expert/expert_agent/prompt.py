"""Validated, one-pass prompt template rendering."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

PLACEHOLDER = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


def load_template(path: Path, *, required: set[str]) -> str:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    template = path.read_text(encoding="utf-8")
    found = set(PLACEHOLDER.findall(template))
    missing = sorted(required - found)
    unknown = sorted(found - required)
    if missing:
        raise ValueError(f"prompt template is missing placeholders: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"prompt template has unknown placeholders: {', '.join(unknown)}")
    return template


def render_template(template: str, values: Mapping[str, str], *, required: set[str] | None = None) -> str:
    expected = set(values) if required is None else required
    found = set(PLACEHOLDER.findall(template))
    missing = sorted(expected - found)
    unknown = sorted(found - set(values))
    if missing:
        raise ValueError(f"prompt template is missing placeholders: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"no value supplied for placeholders: {', '.join(unknown)}")
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def load_and_render(path: Path, values: Mapping[str, str]) -> str:
    return render_template(load_template(path, required=set(values)), values)
