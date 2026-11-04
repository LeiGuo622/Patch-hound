#!/usr/bin/python3
"""Enter the restricted Agent shell with an unprivileged Landlock ruleset."""

from __future__ import annotations

import ctypes
import os
import sys

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


class ScmpArgCmp(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int), ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def _install_network_seccomp() -> None:
    """Deny non-local sockets and network operations for this process tree."""
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ScmpArgCmp)]
    seccomp.seccomp_rule_add_array.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    context = seccomp.seccomp_init(0x7FFF0000)
    if not context:
        raise RuntimeError("seccomp_init failed")
    try:
        deny = 0x00050000 | 1
        for name in (
            b"connect", b"accept", b"accept4", b"bind", b"listen", b"getpeername",
            b"getsockname", b"shutdown", b"sendto", b"sendmmsg", b"recvmmsg",
            b"getsockopt", b"setsockopt", b"ptrace", b"process_vm_readv",
            b"process_vm_writev", b"io_uring_setup", b"io_uring_enter", b"io_uring_register",
        ):
            syscall = seccomp.seccomp_syscall_resolve_name(name)
            if syscall >= 0:
                result = seccomp.seccomp_rule_add_array(context, deny, syscall, 0, None)
                if result < 0:
                    raise OSError(-result, f"seccomp rule failed for {name.decode()}")
        unix_only = ScmpArgCmp(arg=0, op=1, datum_a=1, datum_b=0)
        for name in (b"socket", b"socketpair"):
            syscall = seccomp.seccomp_syscall_resolve_name(name)
            if syscall < 0:
                raise RuntimeError(f"unable to resolve seccomp syscall {name.decode()}")
            result = seccomp.seccomp_rule_add_array(context, deny, syscall, 1, ctypes.byref(unix_only))
            if result < 0:
                raise OSError(-result, f"seccomp rule failed for {name.decode()}")
        result = seccomp.seccomp_load(context)
        if result < 0:
            raise OSError(-result, "seccomp_load failed")
    finally:
        seccomp.seccomp_release(context)


def _supported_access(abi: int) -> int:
    access = READ_ONLY | BASE_WRITE
    if abi >= 2:
        access |= ACCESS_REFER
    if abi >= 3:
        access |= ACCESS_TRUNCATE
    if abi >= 5:
        access |= ACCESS_IOCTL_DEV
    return access


def _syscall(libc: ctypes.CDLL, number: int, *args: object) -> int:
    result = int(libc.syscall(number, *args))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _add_path_rule(libc: ctypes.CDLL, ruleset_fd: int, path: str, access: int) -> None:
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


def _apply_ruleset() -> None:
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
        for path in ("/usr", "/etc", "/workspace/source", "/proc"):
            _add_path_rule(libc, ruleset_fd, path, READ_ONLY)
        device_access = ACCESS_READ_FILE | ACCESS_WRITE_FILE
        if abi >= 5:
            device_access |= ACCESS_IOCTL_DEV
        _add_path_rule(libc, ruleset_fd, "/dev", device_access)
        _add_path_rule(libc, ruleset_fd, "/scratch", handled)
        _add_path_rule(libc, ruleset_fd, "/tmp", handled)

        if int(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        _syscall(libc, SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        _install_network_seccomp()
    finally:
        os.close(ruleset_fd)


def main() -> int:
    try:
        _apply_ruleset()
    except Exception as exc:  # noqa: BLE001 - fail closed with one bounded diagnostic.
        print(f"patchhound Agent shell isolation failed: {exc}", file=sys.stderr)
        return 126

    os.environ.clear()
    os.environ.update(
        {
            "HOME": "/scratch/home",
            "XDG_CONFIG_HOME": "/scratch/xdg-config",
            "XDG_CACHE_HOME": "/scratch/xdg-cache",
            "XDG_DATA_HOME": "/scratch/xdg-data",
            "TMPDIR": "/scratch/tmp",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SHELL": "/bin/bash",
        }
    )
    os.chdir("/workspace/source")
    os.execv("/bin/bash", ["/bin/bash", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
