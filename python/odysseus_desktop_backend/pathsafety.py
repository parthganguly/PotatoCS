from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path

_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_link(path: str | Path) -> bool | None:
    """True if the entry is a symlink or a Windows reparse point (junction).

    Reparse points cover junctions and mount points, which `is_symlink()`
    alone misses on Windows. Returns None when the entry cannot be inspected
    at all — callers must treat None as "skip, fail closed", never as a
    regular file.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat_module.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)
