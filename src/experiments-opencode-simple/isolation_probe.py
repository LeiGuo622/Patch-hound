#!/usr/bin/env python3
"""Run a no-model probe of the nested OpenCode/Agent bubblewrap boundary."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from opencode_simple_agent.isolation import create_layout, run_isolation_probe


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="patchhound-opencode-isolation-source-") as source_raw:
        with tempfile.TemporaryDirectory(prefix="patchhound-opencode-isolation-runtime-") as runtime_raw:
            source = Path(source_raw)
            (source / "readable.c").write_text("int probe;\n", encoding="utf-8")
            result = run_isolation_probe(source, create_layout(Path(runtime_raw)))
            print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
            return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

