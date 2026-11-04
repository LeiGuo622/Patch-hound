"""Load, validate, select, and describe the local review-guidance corpus."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

INDEX_RELATIVE_PATH = Path("subsystem/subsystem.md")
FILE_COLUMN = re.compile(r"^\|.*\|\s*([^|`]+\.md)\s*\|\s*$")
INDEX_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|.*\|\s*([^|`]+\.md)\s*\|\s*$")
STAGE_FIXED_FILES: dict[str, tuple[str, ...]] = {
    "execution_flow": ("callstack.md", "technical-patterns.md"),
    "resource_management": (),
    "locking_synchronization": ("subsystem/locking.md",),
    "security_audit": (),
    "hardware_driver_review": (),
    "final_synthesis": ("false-positive-guide.md",),
}


@dataclass(frozen=True)
class GuidanceDocument:
    relative_path: str
    content: str
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class GuidanceCorpus:
    root: Path
    index: GuidanceDocument
    allowlist: tuple[str, ...]
    revision: str

    def validate_selection(self, values: Iterable[str]) -> tuple[str, ...]:
        allowed = set(self.allowlist)
        selected: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                raise ValueError("selected guidance names must be strings")
            canonical = value if value in allowed else self._canonical_basename(value)
            if canonical is None:
                if not value or value != Path(value).name or "/" in value or "\\" in value or ".." in value:
                    raise ValueError(f"invalid guidance basename: {value!r}")
                raise ValueError(f"guidance file is not in the subsystem index: {value}")
            selected.add(canonical)
        return tuple(name for name in self.allowlist if name in selected)

    def _canonical_basename(self, value: str) -> str | None:
        if not value:
            return None
        candidate = f"{value}.md" if not Path(value).suffix else value
        matches = [name for name in self.allowlist if name.casefold() == candidate.casefold()]
        if len(matches) == 1:
            return matches[0]

        alias_value = value[:-3] if value.casefold().endswith(".md") else value
        alias = alias_value.casefold()
        alias_matches: set[str] = set()
        for line in self.index.content.splitlines():
            match = INDEX_ROW.match(line)
            if match and match.group(1).strip().strip("`").casefold() == alias:
                basename = match.group(2).strip()
                if basename in self.allowlist:
                    alias_matches.add(basename)
        return next(iter(alias_matches)) if len(alias_matches) == 1 else None

    def selected_documents(self, names: Iterable[str]) -> tuple[GuidanceDocument, ...]:
        return tuple(self.load(f"subsystem/{name}") for name in self.validate_selection(names))

    def documents_for(self, consumer: str, selected: Iterable[str]) -> tuple[GuidanceDocument, ...]:
        shared = [doc for doc in self.selected_documents(selected) if doc.relative_path != "subsystem/locking.md"]
        fixed = [self.load(path) for path in STAGE_FIXED_FILES.get(consumer, ())]
        result: list[GuidanceDocument] = []
        seen: set[str] = set()
        for doc in (*shared, *fixed):
            if doc.relative_path not in seen:
                seen.add(doc.relative_path)
                result.append(doc)
        return tuple(result)

    def load(self, relative_path: str) -> GuidanceDocument:
        path = _contained(self.root, relative_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        content = path.read_text(encoding="utf-8")
        raw = content.encode("utf-8")
        return GuidanceDocument(relative_path, content, hashlib.sha256(raw).hexdigest(), len(raw))


def load_corpus(root: Path) -> GuidanceCorpus:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    index_path = _contained(root, INDEX_RELATIVE_PATH.as_posix())
    if not index_path.is_file():
        raise FileNotFoundError(index_path)
    index_content = index_path.read_text(encoding="utf-8")
    allowlist: list[str] = []
    for line in index_content.splitlines():
        match = FILE_COLUMN.match(line)
        if not match:
            continue
        basename = match.group(1).strip()
        if basename != Path(basename).name or "/" in basename or "\\" in basename or ".." in basename:
            raise ValueError(f"subsystem index must list guidance basenames: {basename!r}")
        if basename not in allowlist:
            path = _contained(root, f"subsystem/{basename}")
            if not path.is_file():
                raise FileNotFoundError(path)
            allowlist.append(basename)
    if not allowlist:
        raise ValueError(f"no guidance files found in index: {index_path}")
    raw = index_content.encode("utf-8")
    return GuidanceCorpus(
        root=root,
        index=GuidanceDocument(INDEX_RELATIVE_PATH.as_posix(), index_content, hashlib.sha256(raw).hexdigest(), len(raw)),
        allowlist=tuple(allowlist),
        revision=guidance_revision(root),
    )


def guidance_revision(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.md"), key=lambda item: item.relative_to(root).as_posix()):
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def format_documents(documents: Iterable[GuidanceDocument]) -> str:
    parts = []
    for doc in documents:
        parts.append(f'<review_document path="{doc.relative_path}">\n{doc.content.rstrip()}\n</review_document>')
    return "\n\n".join(parts) if parts else "(none selected)"


def _contained(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"guidance path escapes root: {relative_path}")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"guidance path escapes root: {relative_path}") from exc
    return candidate
