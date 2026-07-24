"""Shared directory/path-safety primitives for Colibrì Stage 2A.

Both the download/conversion orchestrator and the real one-token runner must
prove, before any filesystem write, network call, converter invocation, or
process creation, that every directory they are about to touch is an
ordinary local directory -- never a symlink, junction, or other reparse
point, anywhere in its chain down to the drive/root anchor -- and that every
prospective file path they are about to open is a direct, non-reparse child
of one of those approved directories. This module exists only to keep that
proof identical wherever it is needed; it never itself downloads, converts,
opens a file for writing, or launches a process.
"""

from __future__ import annotations

import os
from pathlib import Path

from odysseus_desktop_backend.services.colibri_stage2_common import ColibriStage2Failure

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def require_ordinary_directory(
    directory: Path, *, missing_category: str, reparse_category: str
) -> Path:
    """Fail closed unless ``directory`` -- and every existing ancestor down
    to its drive/root anchor -- is an ordinary directory: present, a real
    directory, and never a symlink/junction/reparse point.

    Returns the resolved, absolute directory path on success.
    """

    try:
        is_dir = directory.is_dir()
    except OSError as exc:
        raise ColibriStage2Failure(missing_category) from exc
    if not is_dir:
        raise ColibriStage2Failure(missing_category)
    if _is_reparse_point(directory):
        raise ColibriStage2Failure(reparse_category)

    try:
        resolved = directory.resolve(strict=True)
    except OSError as exc:
        raise ColibriStage2Failure(missing_category) from exc
    if _is_reparse_point(resolved):
        raise ColibriStage2Failure(reparse_category)

    anchor = Path(resolved.anchor)
    ancestor = resolved.parent
    while True:
        if _is_reparse_point(ancestor):
            raise ColibriStage2Failure(reparse_category)
        if ancestor == anchor or ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent
    return resolved


def require_direct_child_path(resolved_directory: Path, basename: str, *, category: str) -> Path:
    """Fail closed unless ``resolved_directory / basename`` is a safe,
    direct child of an already-approved (``require_ordinary_directory``'d)
    directory: no separators or dot-segments in ``basename``, the
    prospective path is not itself a reparse point, and it resolves as a
    direct child rather than escaping via a symlinked ancestor.

    ``basename`` need not already exist -- this also validates prospective
    output paths that are about to be created.
    """

    if (
        not basename
        or os.sep in basename
        or (os.altsep and os.altsep in basename)
        or basename in (".", "..")
    ):
        raise ColibriStage2Failure(category)
    candidate = resolved_directory / basename
    if _is_reparse_point(candidate):
        raise ColibriStage2Failure(category)
    if candidate.parent != resolved_directory:
        raise ColibriStage2Failure(category)
    return candidate
