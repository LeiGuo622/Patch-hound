#!/usr/bin/env python3
"""Run the Codex app-server under an external Landlock policy.

The managed terminal used for this project does not permit a nested bubblewrap
user namespace.  This helper is deliberately small and fail-closed: the
app-server is started only after the caller supplied explicit read and write
roots have been installed in a Landlock ruleset.  The app-server itself is
configured with Codex's external/full-access mode, while this process remains
the filesystem boundary.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

ACCESS_EXECUTE = 1 << 0
ACCESS_WRITE_FILE = 1 << 1
ACCESS_READ_FILE = 1 << 2
ACCESS_READ_DIR = 1 << 3
ACCESS_REMOVE_DIR = 1 << 4
ACCESS_REMOVE_FILE = 1 << 5
ACCESS_MAKE_CHAR = 1 << 6
ACCESS_MAKE_DIR = 1 << 7
ACCESS_MAKE_REG = 1 << 8
ACCESS_MAKE_SOCK = 1 << 9
ACCESS_MAKE_FIFO = 1 << 10
ACCESS_MAKE_BLOCK = 1 << 11
ACCESS_MAKE_SYM = 1 << 12
ACCESS_REFER = 1 << 13
ACCESS_TRUNCATE = 1 << 14
ACCESS_IOCTL_DEV = 1 << 15

READ_ONLY = ACCESS_EXECUTE | ACCESS_READ_FILE | ACCESS_READ_DIR
BASE_WRITE = (
    ACCESS_WRITE_FILE
    | ACCESS_REMOVE_DIR
    | ACCESS_REMOVE_FILE
    | ACCESS_MAKE_CHAR
    | ACCESS_MAKE_DIR
    | ACCESS_MAKE_REG
    | ACCESS_MAKE_SOCK
    | ACCESS_MAKE_FIFO
    | ACCESS_MAKE_BLOCK
    | ACCESS_MAKE_SYM
)


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]


def _syscall(libc: ctypes.CDLL, number: int, *args: object) -> int:
    result = int(libc.syscall(number, *args))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _supported_access(abi: int) -> int:
    access = READ_ONLY | BASE_WRITE
    if abi >= 2:
        access |= ACCESS_REFER
    if abi >= 3:
        access |= ACCESS_TRUNCATE
    if abi >= 5:
        access |= ACCESS_IOCTL_DEV
    return access


def _paths(name: str, *, required: bool) -> tuple[Path, ...]:
    raw = os.environ.pop(name, "")
    values: list[Path] = []
    for value in raw.split(os.pathsep):
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if not path.is_absolute():
            raise RuntimeError(f"{name} contains a non-absolute path: {value!r}")
        if not path.exists():
            raise RuntimeError(f"{name} contains a missing path: {path}")
        if path not in values:
            values.append(path)
    if required and not values:
        raise RuntimeError(f"{name} must contain at least one path")
    return tuple(values)


def _add_path_rule(libc: ctypes.CDLL, ruleset_fd: int, path: Path, access: int) -> None:
    path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        attr = PathBeneathAttr(allowed_access=access, parent_fd=path_fd)
        _syscall(
            libc,
            SYS_LANDLOCK_ADD_RULE,
            ruleset_fd,
            LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(attr),
            0,
        )
    finally:
        os.close(path_fd)


def _apply_ruleset(read_paths: tuple[Path, ...], write_paths: tuple[Path, ...]) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    abi = _syscall(libc, SYS_LANDLOCK_CREATE_RULESET, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
    if abi < 1:
        raise RuntimeError(f"Landlock ABI is unavailable: {abi}")
    handled = _supported_access(abi)
    attr = RulesetAttr(handled_access_fs=handled)
    ruleset_fd = _syscall(
        libc,
        SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.sizeof(attr),
        0,
    )
    try:
        # Permit pathname traversal only.  Every readable or writable subtree
        # below this root is granted by a more-specific rule; unlisted files
        # remain unreadable and unwritable.
        _add_path_rule(libc, ruleset_fd, Path("/"), ACCESS_EXECUTE)
        for path in read_paths:
            _add_path_rule(libc, ruleset_fd, path, READ_ONLY)
        for path in write_paths:
            _add_path_rule(libc, ruleset_fd, path, handled)
        if int(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        _syscall(libc, SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)


def main() -> int:
    try:
        real_bin = Path(os.environ.pop("PATCHHOUND_REAL_CODEX_BIN", "")).expanduser().resolve()
        if not real_bin.is_file() or not os.access(real_bin, os.X_OK):
            raise RuntimeError(f"Codex executable is missing or not executable: {real_bin}")
        read_paths = _paths("PATCHHOUND_LANDLOCK_READ_PATHS", required=True)
        write_paths = _paths("PATCHHOUND_LANDLOCK_WRITE_PATHS", required=True)
        _apply_ruleset(read_paths, write_paths)
    except Exception as exc:  # noqa: BLE001 - one bounded fail-closed diagnostic.
        print(f"PatchHound external Codex isolation failed: {exc}", file=sys.stderr)
        return 126

    os.execv(str(real_bin), [str(real_bin), *sys.argv[1:]])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
