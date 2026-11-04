"""Small git helpers used by PatchHound."""

from .commit_info import get_commit_diff, get_commit_message

__all__ = ["get_commit_diff", "get_commit_message"]
