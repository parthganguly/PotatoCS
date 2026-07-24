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
import stat as stat_module
import sys
from pathlib import Path

from odysseus_desktop_backend.services.colibri_stage2_common import ColibriStage2Failure

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_symlink_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat_module.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def require_ordinary_directory(
    directory: Path, *, missing_category: str, reparse_category: str
) -> Path:
    """Fail closed unless ``directory`` -- and every existing ancestor down
    to its drive/root anchor -- is an ordinary directory: present, a real
    directory, and never a symlink/junction/reparse point.

    The *original*, lexical path is inspected via ``lstat`` first, walking
    from the directory itself up to its anchor, strictly before any
    resolution occurs. Resolving first and only then walking the resolved
    path's ancestors would erase a symlinked/junctioned ancestor segment
    from the chain being inspected -- resolution silently replaces it with
    its real target before the walk ever sees the original segment, which
    is exactly the kind of escape this function exists to catch. Only
    after the entire original chain passes is the path resolved, and the
    resolution is then itself checked to confirm it landed nowhere but
    this same, already-approved location.

    Returns the resolved, absolute directory path on success.
    """

    original = Path(directory)
    if not original.is_absolute():
        raise ColibriStage2Failure(missing_category)

    node = original
    while True:
        if _is_symlink_or_reparse(node):
            raise ColibriStage2Failure(reparse_category)
        parent = node.parent
        if parent == node:
            break
        node = parent

    try:
        is_dir = original.is_dir()
    except OSError as exc:
        raise ColibriStage2Failure(missing_category) from exc
    if not is_dir:
        raise ColibriStage2Failure(missing_category)

    try:
        resolved = original.resolve(strict=True)
    except OSError as exc:
        raise ColibriStage2Failure(missing_category) from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(original)):
        # The original chain had no symlink/reparse point anywhere, so a
        # resolution landing somewhere else is unexpected -- e.g. a race,
        # or an OS-level substitution invisible to lstat. Reject either way.
        raise ColibriStage2Failure(reparse_category)
    return resolved


def require_direct_child_path(resolved_directory: Path, basename: str, *, category: str) -> Path:
    """Fail closed unless ``resolved_directory / basename`` is a safe,
    direct child of an already-approved (``require_ordinary_directory``'d)
    directory: no ``/`` or ``\\`` (on every platform, not just the local
    one), no drive-qualification, no dot/dot-dot names, the prospective
    path is not itself a reparse point, and it resolves as a direct child
    rather than escaping via a symlinked ancestor.

    ``basename`` need not already exist -- this also validates prospective
    output paths that are about to be created.
    """

    if (
        not basename
        or "/" in basename
        or "\\" in basename
        or ":" in basename
        or basename in (".", "..")
    ):
        raise ColibriStage2Failure(category)
    candidate = resolved_directory / basename
    if _is_symlink_or_reparse(candidate):
        raise ColibriStage2Failure(category)
    if candidate.parent != resolved_directory:
        raise ColibriStage2Failure(category)
    return candidate


def atomic_no_replace_move(source: Path, destination: Path, *, exists_category: str) -> None:
    """Move ``source`` to ``destination`` with true no-replace semantics.

    This never silently overwrites an existing destination: a destination
    created by a concurrent racer immediately before this call survives
    untouched, and this call fails closed with ``exists_category`` instead.
    Callers are responsible for ensuring both paths sit inside directories
    already proven safe by ``require_ordinary_directory``/
    ``require_direct_child_path`` -- this function performs the move only.

    On Windows, ``os.rename()`` (unlike ``os.replace()``) already refuses
    to replace an existing destination file -- CPython's Windows
    implementation calls ``MoveFileExW`` without
    ``MOVEFILE_REPLACE_EXISTING``, so it raises ``FileExistsError``
    (WinError 183) if the destination exists. That native no-replace
    rename is used directly. On other platforms, ``os.rename()`` silently
    replaces an existing destination (same as ``os.replace()``), so a
    hardlink-then-unlink sequence is used instead: ``os.link()`` itself is
    the atomic no-replace step (POSIX ``link()`` fails with ``EEXIST`` if
    the destination exists), and the source is removed only once that
    link has actually succeeded.
    """

    if sys.platform == "win32":
        try:
            os.rename(source, destination)
        except FileExistsError as exc:
            raise ColibriStage2Failure(exists_category) from exc
        except OSError as exc:
            if getattr(exc, "winerror", None) == 183:  # ERROR_ALREADY_EXISTS
                raise ColibriStage2Failure(exists_category) from exc
            raise
    else:
        try:
            os.link(source, destination)
        except FileExistsError as exc:
            raise ColibriStage2Failure(exists_category) from exc
        source.unlink()
