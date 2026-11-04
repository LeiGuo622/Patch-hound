#!/usr/bin/env python3
"""Run one prepared Case through the simplified PatchHound Codex agent."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from simple_agent.case_io import load_case
from simple_agent.config import DEFAULT_CASES_DIR, DEFAULT_PROMPT_TEMPLATE, DEFAULT_RUNTIME_CONFIG
from simple_agent.prompt import load_case_prompt
from simple_agent.workflow import run_case


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id", help="Prepared Case ID, for example case_0001")
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--prompt-template", type=Path, default=DEFAULT_PROMPT_TEMPLATE)
    parser.add_argument("--codex-runtime-config", type=Path, default=DEFAULT_RUNTIME_CONFIG)
    parser.add_argument("--run-id", default=None, help="Explicit run ID such as run_0001")
    parser.add_argument("--force-run", action="store_true", help="Replace an existing explicit --run-id")
    parser.add_argument("--model", default=None, help="Optional Codex model override")
    parser.add_argument("--codex-bin", default=None, help="Optional Codex CLI path override")
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--max-contract-repairs", type=int, default=2)
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
    parser.add_argument("--quiet", action="store_true", help="Suppress live Codex progress and response output")
    parser.add_argument("--dry-run", action="store_true", help="Render the prompt without writes or Codex startup")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.dry_run:
            case = load_case(args.case_id, args.cases_dir)
            prompt = load_case_prompt(case, args.prompt_template)
            print("[patchhound-simple] dry run; Codex and Phoenix were not started")
            print("[user] input:")
            print(prompt.rstrip())
            print("[user] end")
            return 0

        result = run_case(
            args.case_id,
            cases_dir=args.cases_dir,
            prompt_template=args.prompt_template,
            runtime_config=args.codex_runtime_config,
            run_id=args.run_id,
            force_run=args.force_run,
            model=args.model,
            codex_bin=args.codex_bin,
            timeout_seconds=args.timeout_seconds,
            max_contract_repairs=args.max_contract_repairs,
            use_history_tool=not args.no_history_tool,
            history_linux_repo=args.history_linux_repo,
            env_path=args.env_file,
            phoenix_enabled=args.phoenix,
            phoenix_endpoint=args.phoenix_endpoint,
            phoenix_project=args.phoenix_project,
            save_conversation=args.save_conversation,
            live_output=not args.quiet,
        )
        print(
            f"[patchhound-simple] completed case={args.case_id} assessment={result.assessment} "
            f"output={result.output_path}"
        )
        print(f"[patchhound-simple] thread_id={result.thread_id} turn_ids={','.join(result.turn_ids)}")
        if result.phoenix_trace_id:
            print(f"[patchhound-simple] phoenix_trace_id={result.phoenix_trace_id}")
        return 0
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns a useful nonzero status.
        print(f"[patchhound-simple] failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
