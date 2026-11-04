from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from expert_agent.guidance import guidance_revision, load_corpus  # noqa: E402


def make_corpus(root: Path) -> None:
    subsystem = root / "subsystem"
    subsystem.mkdir(parents=True)
    (subsystem / "subsystem.md").write_text(
        "| Subsystem | Triggers | File |\n|---|---|---|\n| Net | net/ | net.md |\n| Net again | skb | net.md |\n"
        "| Lock | spin | locking.md |\n",
        encoding="utf-8",
    )
    (subsystem / "net.md").write_text("network guide\n", encoding="utf-8")
    (subsystem / "locking.md").write_text("locking guide\n", encoding="utf-8")
    (root / "callstack.md").write_text("callstack\n", encoding="utf-8")
    (root / "technical-patterns.md").write_text("patterns\n", encoding="utf-8")
    (root / "false-positive-guide.md").write_text("false positive\n", encoding="utf-8")


class GuidanceTest(unittest.TestCase):
    def test_allowlist_deduplicates_and_selection_uses_index_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_corpus(root)
            corpus = load_corpus(root)
            self.assertEqual(corpus.allowlist, ("net.md", "locking.md"))
            self.assertEqual(corpus.validate_selection(["locking.md", "net.md"]), ("net.md", "locking.md"))

    def test_rejects_paths_and_unknown_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_corpus(root)
            corpus = load_corpus(root)
            for value in ("subsystem/net.md", "../net.md", "/tmp/net.md", "unknown.md"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    corpus.validate_selection([value])

    def test_stage_injection_matrix_keeps_locking_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_corpus(root)
            corpus = load_corpus(root)
            execution = [doc.relative_path for doc in corpus.documents_for("execution_flow", ["net.md", "locking.md"])]
            locking = [doc.relative_path for doc in corpus.documents_for("locking_synchronization", ["net.md", "locking.md"])]
            final = [doc.relative_path for doc in corpus.documents_for("final_synthesis", ["net.md", "locking.md"])]
            self.assertEqual(execution, ["subsystem/net.md", "callstack.md", "technical-patterns.md"])
            self.assertEqual(locking, ["subsystem/net.md", "subsystem/locking.md"])
            self.assertEqual(final, ["subsystem/net.md", "false-positive-guide.md"])

    def test_revision_changes_with_any_markdown_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_corpus(root)
            before = guidance_revision(root)
            (root / "callstack.md").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(before, guidance_revision(root))


if __name__ == "__main__":
    unittest.main()
