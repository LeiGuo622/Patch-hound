#!/usr/bin/env python3
"""Run prepared Cases sequentially through experiments-omp-simple."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omp_agent.batch import parse_case_id, run_batch
from omp_agent.config import DEFAULT_CASES_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    range_group = parser.add_mutually_exclusive_group()
    range_group.add_argument("--start", type=parse_case_id, default=None)
    range_group.add_argument("--after", type=parse_case_id, default=None)
    parser.add_argument("--end", type=parse_case_id, default=None)
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--wait-active", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("run_case_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    forwarded = list(args.run_case_args)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    try:
        result = run_batch(
            cases_dir=args.cases_dir,
            start=args.start,
            after=args.after,
            end=args.end,
            wait_active=args.wait_active,
            poll_seconds=args.poll_seconds,
            dry_run=args.dry_run,
            run_case_args=forwarded,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[patchhound-omp-simple-batch] failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "[patchhound-omp-simple-batch] summary: "
        f"succeeded={len(result.succeeded)} failed={len(result.failed)} skipped={len(result.skipped)}"
    )
    if result.failed:
        print(f"[patchhound-omp-simple-batch] failed cases: {','.join(result.failed)}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

