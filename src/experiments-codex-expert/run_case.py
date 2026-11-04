#!/usr/bin/env python3
"""Run one prepared Case through the staged PatchHound Codex agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from expert_agent.config import DEFAULT_CASES_DIR, DEFAULT_GUIDANCE_ROOT, DEFAULT_RUNTIME_CONFIG
from expert_agent.stage_output import OPTIONAL_STAGE_IDS
from expert_agent.workflow import preview_case, run_case


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id", help="Prepared Case ID, for example case_0001")
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--codex-runtime-config", type=Path, default=DEFAULT_RUNTIME_CONFIG)
    parser.add_argument("--guidance-root", type=Path, default=DEFAULT_GUIDANCE_ROOT)
    parser.add_argument("--run-id", default=None, help="Explicit run ID such as run_0001")
    parser.add_argument("--force-run", action="store_true", help="Replace an existing explicit --run-id")
    parser.add_argument("--output-root", type=Path, help="Test-only output directory override")
    parser.add_argument("--model", default=None, help="Optional Codex model override")
    parser.add_argument("--codex-bin", default=None, help="Optional Codex CLI path override")
    parser.add_argument("--timeout-seconds", type=int, default=7200, help="Total timeout for the entire Case")
    parser.add_argument("--no-history-tool", action="store_true")
    parser.add_argument("--history-linux-repo", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=None, help="Phoenix/Linux repository environment file")
    parser.add_argument(
        "--phoenix",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable Phoenix; default: PHOENIX_ENABLED or true",
    )
    parser.add_argument("--phoenix-endpoint", default=None)
    parser.add_argument("--phoenix-project", default=None)
    parser.add_argument("--save-conversation", action="store_true")
    parser.add_argument(
        "--stage",
        action="append",
        choices=OPTIONAL_STAGE_IDS,
        default=None,
        help="Manually select a conditional stage; repeatable and skips planning",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress live Codex progress and response output")
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarize prompts without writes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.dry_run:
            preview = preview_case(
                args.case_id,
                cases_dir=args.cases_dir,
                runtime_config=args.codex_runtime_config,
                guidance_root=args.guidance_root,
                model=args.model,
                manual_stages=args.stage,
            )
            print("[patchhound-expert] dry run; no run was allocated and Codex, MCP, and Phoenix were not started")
            print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        result = run_case(
            args.case_id,
            cases_dir=args.cases_dir,
            runtime_config=args.codex_runtime_config,
            guidance_root=args.guidance_root,
            run_id=args.run_id,
            force_run=args.force_run,
            output_root=args.output_root,
            model=args.model,
            codex_bin=args.codex_bin,
            timeout_seconds=args.timeout_seconds,
            use_history_tool=not args.no_history_tool,
            history_linux_repo=args.history_linux_repo,
            env_path=args.env_file,
            phoenix_enabled=args.phoenix,
            phoenix_endpoint=args.phoenix_endpoint,
            phoenix_project=args.phoenix_project,
            save_conversation=args.save_conversation,
            live_output=not args.quiet,
            manual_stages=args.stage,
        )
        print(
            f"[patchhound-expert] completed case={args.case_id} assessment={result.assessment or 'unparsed'} "
            f"output={result.output_path}"
        )
        print(
            f"[patchhound-expert] thread_ids={json.dumps(result.thread_ids, sort_keys=True)} "
            f"turn_ids={','.join(result.turn_ids)}"
        )
        if result.phoenix_trace_id:
            print(f"[patchhound-expert] phoenix_trace_id={result.phoenix_trace_id}")
        return 0
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[patchhound-expert] failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
