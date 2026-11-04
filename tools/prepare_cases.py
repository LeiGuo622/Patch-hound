"""Prepare leak-reduced PatchHound cases from a CSV of Linux commits."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def _find_project_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("PatchHound project root not found")


PROJECT_ROOT = _find_project_root()
SRC_DIR = PROJECT_ROOT
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tools import get_commit_diff, get_commit_message  # noqa: E402
from tools.commit_info import LINUX_REPO_ENV  # noqa: E402

DEFAULT_CASES_DIR = PROJECT_ROOT / "cases"
DEFAULT_METADATA_DIR = PROJECT_ROOT / "cases" / "case_metadata"
CASE_ID_PATTERN = re.compile(r"^case_([0-9]{4,})$")


@dataclass(frozen=True)
class CommitRow:
    row_number: int
    commit_id: str

    @property
    def case_id(self) -> str:
        return f"case_{self.row_number:04d}"


def read_commit_rows(csv_path: Path) -> list[CommitRow]:
    """Read commit_id values by column name from a CSV file."""

    rows: list[CommitRow] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header row: {csv_path}")
        if "commit_id" not in reader.fieldnames:
            columns = ", ".join(reader.fieldnames)
            raise ValueError(f"CSV must contain a commit_id column; found: {columns}")

        for row_number, row in enumerate(reader, start=1):
            commit_id = (row.get("commit_id") or "").strip()
            if not commit_id:
                raise ValueError(f"CSV row {row_number} has an empty commit_id")
            rows.append(CommitRow(row_number=row_number, commit_id=commit_id))

    return rows


def prepare_cases(
    csv_path: Path,
    *,
    linux_repo: Path,
    cases_dir: Path = DEFAULT_CASES_DIR,
    metadata_dir: Path = DEFAULT_METADATA_DIR,
    force: bool = False,
) -> list[Path]:
    """Create case directories and metadata files for every row in csv_path."""

    linux_repo = linux_repo.expanduser().resolve()
    cases_dir = cases_dir.expanduser().resolve()
    metadata_dir = metadata_dir.expanduser().resolve()

    if not linux_repo.is_dir():
        raise RuntimeError(f"Linux repository does not exist: {linux_repo}")

    os.environ[LINUX_REPO_ENV] = str(linux_repo)
    rows = read_commit_rows(csv_path)

    metadata_dir.mkdir(parents=True, exist_ok=True)
    next_case_number = find_next_case_number(cases_dir, metadata_dir)
    written_metadata: list[Path] = []
    for offset, row in enumerate(rows):
        case_number = next_case_number + offset
        case_id = format_case_id(case_number)
        metadata_path = _prepare_one_case(
            row,
            case_id=case_id,
            case_number=case_number,
            linux_repo=linux_repo,
            cases_dir=cases_dir,
            metadata_dir=metadata_dir,
            force=force,
        )
        written_metadata.append(metadata_path)

    return written_metadata


def format_case_id(case_number: int) -> str:
    if case_number < 1:
        raise ValueError("case_number must be at least 1")
    return f"case_{case_number:04d}"


def find_next_case_number(cases_dir: Path, metadata_dir: Path) -> int:
    """Return the next global case number after existing case dirs and metadata."""

    highest = 0
    for parent, suffix in ((cases_dir, ""), (metadata_dir, ".json")):
        if not parent.exists():
            continue
        for path in parent.iterdir():
            name = path.name
            if suffix:
                if not name.endswith(suffix):
                    continue
                name = name[: -len(suffix)]
            match = CASE_ID_PATTERN.fullmatch(name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def _prepare_one_case(
    row: CommitRow,
    *,
    case_id: str,
    case_number: int,
    linux_repo: Path,
    cases_dir: Path,
    metadata_dir: Path,
    force: bool,
) -> Path:
    case_dir = cases_dir / case_id
    source_dir = case_dir / "source"
    input_dir = case_dir / "input"
    output_dir = case_dir / "output"
    metadata_path = metadata_dir / f"{case_id}.json"

    if case_dir.exists():
        if not force:
            raise RuntimeError(f"Case directory already exists; use --force to replace it: {case_dir}")
        shutil.rmtree(case_dir)
    if metadata_path.exists():
        if not force:
            raise RuntimeError(f"Metadata already exists; use --force to replace it: {metadata_path}")
        metadata_path.unlink()

    source_dir.mkdir(parents=True)
    input_dir.mkdir()
    output_dir.mkdir()

    _extract_git_archive(linux_repo, row.commit_id, source_dir)
    if (source_dir / ".git").exists():
        raise RuntimeError(f"git archive unexpectedly created a .git directory in {source_dir}")

    (input_dir / "commit_message.txt").write_text(
        get_commit_message(row.commit_id).rstrip() + "\n",
        encoding="utf-8",
    )
    (input_dir / "diff.txt").write_text(
        get_commit_diff(row.commit_id).rstrip() + "\n",
        encoding="utf-8",
    )

    metadata = {
        "case_id": case_id,
        "case_number": case_number,
        "csv_row_number": row.row_number,
        "row_number": row.row_number,
        "commit_id": row.commit_id,
        "archive_commit": row.commit_id,
        "source_dir": _project_relative(source_dir),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return metadata_path


def _extract_git_archive(linux_repo: Path, commit_id: str, source_dir: Path) -> None:
    archive = subprocess.Popen(
        ["git", "-C", str(linux_repo), "archive", commit_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    try:
        tar = subprocess.run(
            ["tar", "-x", "-C", str(source_dir)],
            stdin=archive.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        archive.stdout.close()

    archive_stderr = archive.stderr.read().decode(errors="replace") if archive.stderr else ""
    archive_returncode = archive.wait()
    if archive_returncode != 0:
        raise RuntimeError(f"git archive failed for {commit_id}: {archive_stderr.strip()}")
    if tar.returncode != 0:
        tar_stderr = tar.stderr.decode(errors="replace")
        raise RuntimeError(f"tar extraction failed for {commit_id}: {tar_stderr.strip()}")


def _project_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def _linux_repo_from_env() -> Path:
    load_dotenv(find_dotenv(usecwd=True))
    value = os.environ.get(LINUX_REPO_ENV)
    if not value:
        raise RuntimeError(f"{LINUX_REPO_ENV} is not configured; pass --linux-repo or set it in .env")
    return Path(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="CSV file with a commit_id column")
    parser.add_argument(
        "--linux-repo",
        type=Path,
        default=None,
        help=f"Linux git repository path; defaults to {LINUX_REPO_ENV} from .env",
    )
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR)
    parser.add_argument("--force", action="store_true", help="Replace existing generated case directories")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    csv_path = args.csv_path.expanduser().resolve()
    linux_repo = args.linux_repo if args.linux_repo is not None else _linux_repo_from_env()

    metadata_paths = prepare_cases(
        csv_path,
        linux_repo=linux_repo,
        cases_dir=args.cases_dir,
        metadata_dir=args.metadata_dir,
        force=args.force,
    )
    for path in metadata_paths:
        print(f"prepared {path.stem}: metadata={_project_relative(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
