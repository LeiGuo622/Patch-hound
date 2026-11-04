from __future__ import annotations

from pathlib import Path

import pytest

from opencode_expert_agent.guidance import GuidanceCorpus, GuidanceDocument


def corpus(*names: str, rows: tuple[tuple[str, str], ...] = ()) -> GuidanceCorpus:
    content = "\n".join(f"| {label} | triggers | {filename} |" for label, filename in rows)
    index = GuidanceDocument("subsystem/subsystem.md", content, "", len(content))
    return GuidanceCorpus(root=Path("."), index=index, allowlist=names, revision="test")


def test_validate_selection_canonicalizes_case_and_missing_suffix() -> None:
    guidance = corpus("mm-alloc.md", "vfs.md", "smb-ksmbd.md")

    assert guidance.validate_selection(("VFS.md", "VFS")) == ("vfs.md",)


def test_validate_selection_canonicalizes_unique_subsystem_labels() -> None:
    guidance = corpus(
        "mm-alloc.md",
        "vfs.md",
        "smb-ksmbd.md",
        rows=(
            ("MM Allocation", "mm-alloc.md"),
            ("VFS", "vfs.md"),
            ("SMB/ksmbd", "smb-ksmbd.md"),
        ),
    )

    assert guidance.validate_selection(("VFS", "SMB/ksmbd", "MM Allocation.md")) == (
        "mm-alloc.md",
        "vfs.md",
        "smb-ksmbd.md",
    )


def test_validate_selection_preserves_allowlist_order_and_deduplicates() -> None:
    guidance = corpus("mm-alloc.md", "vfs.md", "smb-ksmbd.md")

    assert guidance.validate_selection(("SMB-KSMBD.MD", "vfs.md", "VFS.md")) == (
        "vfs.md",
        "smb-ksmbd.md",
    )


@pytest.mark.parametrize("value", ("../vfs.md", "subsystem/vfs.md", "unknown", "vfs.txt"))
def test_validate_selection_rejects_unsafe_or_unknown_names(value: str) -> None:
    guidance = corpus("vfs.md")

    with pytest.raises(ValueError):
        guidance.validate_selection((value,))


def test_validate_selection_rejects_ambiguous_casefold_match() -> None:
    guidance = corpus("vfs.md", "VFS.md")

    with pytest.raises(ValueError):
        guidance.validate_selection(("VfS",))
