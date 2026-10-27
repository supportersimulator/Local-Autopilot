"""Shared owner/mode checks for user-only control + ledger directories."""
from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafePath(Exception):
    """Path is a symlink, not owned by us, or group/world accessible."""


def check_private(path: Path, *, is_dir: bool) -> None:
    """Raise UnsafePath unless `path` is a real (non-symlink) dir/file owned by
    the current uid with no group/other permission bits. OSError propagates."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise UnsafePath(f"{path} is a symlink")
    if is_dir and not stat.S_ISDIR(st.st_mode):
        raise UnsafePath(f"{path} is not a directory")
    if not is_dir and not stat.S_ISREG(st.st_mode):
        raise UnsafePath(f"{path} is not a regular file")
    if st.st_uid != os.getuid():
        raise UnsafePath(f"{path} owned by uid {st.st_uid}, expected {os.getuid()}")
    if st.st_mode & 0o077:
        want = "0700" if is_dir else "0600"
        raise UnsafePath(
            f"{path} mode {oct(st.st_mode & 0o777)} is looser than {want}; "
            f"fix with: chmod {want[1:]} '{path}'"
        )


def check_parents_in_home(path: Path) -> None:
    """Parents between $HOME and `path` must not be group/world writable."""
    home = Path(os.path.expanduser("~")).resolve()
    try:
        rel = Path(path).parent.resolve().relative_to(home)
    except ValueError:
        return  # outside $HOME (tests / custom); per-dir check still applies
    cur = home
    for part in rel.parts:
        cur = cur / part
        st = os.lstat(cur)
        if st.st_mode & 0o022:
            raise UnsafePath(f"parent {cur} is group/world writable")


def make_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
