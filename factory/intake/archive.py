"""
factory.intake.archive
=======================
Safe archive handling for the tool intake (Phase 2a).

Extraction is strictly vetted *before* anything is written:
* symlinks, hardlinks, device/FIFO/socket entries — rejected outright
* absolute paths, Windows drive letters, UNC prefixes, ``..`` traversal — rejected
* Windows-unsafe names: ``<>:"|?*``, trailing space/period, reserved device
  names (CON/PRN/AUX/NUL/COM1-9/LPT1-9, any case, with or without
  extension) and NTFS alternate-data-stream names (``file:stream``) — rejected
* duplicate member paths — including case-only and Unicode-normalization
  variants on the normalized collision key (``docs/readme.md`` vs
  ``DOCS/README.MD``) — and file/directory collisions — rejected
* pre-existing destination directories — rejected outright before any
  read/write/delete, so an invalid archive never destroys prior content
* file-count, per-file and total extracted size, and path-length caps — enforced
* partial extractions are cleaned up on any failure (only the destination
  directory this call itself created is ever removed)

Hashing never follows links (``lstat``-based) and produces a deterministic
content-tree hash (relative POSIX paths + per-file SHA-256, sorted).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tarfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from factory.errors import IntakeFetchError

logger = logging.getLogger(__name__)

# Characters forbidden in Windows file/directory names. A ':' in any
# component also enables NTFS alternate data streams (e.g. "SKILL.md:payload"),
# which directory enumeration and scanners may not treat as ordinary files.
_WIN_FORBIDDEN_CHARS = frozenset('<>:"|?*')

# Reserved Windows device names — rejected as a whole component OR as the
# base name before an extension ("CON", "con.txt", "NUL.tar" are all unsafe).
_WIN_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


@dataclass(frozen=True)
class ArchiveCaps:
    """Hard limits applied during extraction."""
    max_archive_bytes: int = 64 * 1024 * 1024
    max_extracted_bytes: int = 256 * 1024 * 1024
    max_file_count: int = 10_000
    max_path_length: int = 240


def hash_file_sha256(path: Path) -> str:
    """SHA-256 of a file's bytes. Raises IntakeFetchError on unreadable files."""
    sha = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
    except OSError as exc:
        raise IntakeFetchError(f"Cannot hash file {path}: {exc}") from exc
    return sha.hexdigest()


def _windows_component_violation(part: str) -> str | None:
    """
    Return a rejection reason when a single path component is unsafe on
    Windows, else None. Checks (case-insensitively where relevant):
    * forbidden characters ``<>:"|?*`` (':' also covers NTFS alternate
      data streams such as ``SKILL.md:payload``)
    * a trailing space or period (Windows silently strips these)
    * reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9), both as
      the whole component and as the base name before an extension
    """
    bad = _WIN_FORBIDDEN_CHARS.intersection(part)
    if bad:
        return f"contains Windows-forbidden character(s) {''.join(sorted(bad))!r}"
    if part.endswith(" ") or part.endswith("."):
        return "ends with a space or period"
    base = part.split(".", 1)[0].upper()
    if base in _WIN_RESERVED_NAMES:
        return "is a reserved Windows device name"
    return None


def _is_safe_relative(name: str, max_path_length: int = 240) -> bool:
    """True when an archive member name is a safe, relative POSIX path."""
    if not name or len(name) > max_path_length:
        return False
    if "\\" in name:
        return False
    if name.startswith("/") or name.startswith("~"):
        return False
    # Windows drive letters ("C:", "c:/") and UNC-ish prefixes.
    if len(name) >= 2 and name[1] == ":":
        return False
    pure = PurePosixPath(name)
    if pure.is_absolute():
        return False
    if any(part in ("", ".", "..") for part in pure.parts):
        return False
    # Every component must also be safe as a Windows file/directory name.
    for part in pure.parts:
        if _windows_component_violation(part) is not None:
            return False
    return True


def _collision_key(name: str) -> str:
    """
    Normalized comparison key for path-collision detection.

    Windows (and macOS) filesystems are case-insensitive, and the same
    visual path can be encoded with different Unicode normalizations (NFC
    vs NFD). Collisions are therefore matched on a Unicode-normalized
    (NFC), case-folded key while the ORIGINAL member names are kept
    untouched for extraction. This detects file/file, directory/directory,
    implicit-directory, and file/directory collisions regardless of member
    ordering — e.g. ``docs/readme.md`` vs ``DOCS/README.MD``.
    """
    return unicodedata.normalize("NFC", name).casefold()


def _vet_members(
    members: list[tarfile.TarInfo],
    caps: ArchiveCaps,
) -> list[tarfile.TarInfo]:
    """
    Validate every member against the safety rules. Fail-closed: any
    offending member aborts the whole extraction with IntakeFetchError.
    Nothing is written until every member passes — including duplicate and
    file/directory-collision checks across the whole member list. All
    collision checks use case-folded, Unicode-normalized keys (see
    ``_collision_key``), so case-only and normalization-only differences
    collide exactly like exact matches on case-insensitive filesystems.
    """
    file_count = 0
    total_size = 0
    # Duplicate / file-vs-directory collision detection. Sets hold
    # _collision_key values; original names are used in error messages.
    files: set[str] = set()
    dirs: set[str] = set()
    # Directories implied by member ancestry — a file must never claim one.
    implied_dirs: set[str] = set()
    for m in members:
        name = m.name.rstrip("/")
        key = _collision_key(name)
        if len(name) > caps.max_path_length:
            raise IntakeFetchError(
                f"Archive member path exceeds the path-length cap "
                f"({caps.max_path_length}): {m.name!r}"
            )
        if not _is_safe_relative(name, caps.max_path_length):
            # Provide the precise Windows-component reason when applicable.
            # Structural parts (''/'.'/'..') fail for traversal/structure
            # reasons, not Windows-name reasons — skip them so a traversal
            # never gets a misleading "trailing space/period" detail.
            reason = None
            if name:
                for part in PurePosixPath(name).parts:
                    if part in ("", ".", ".."):
                        continue
                    reason = _windows_component_violation(part)
                    if reason is not None:
                        break
            detail = f" ({reason})" if reason else ""
            raise IntakeFetchError(
                f"Unsafe archive member path rejected: {m.name!r}{detail}"
            )
        if m.issym() or m.islnk():
            raise IntakeFetchError(
                f"Archive contains a link entry (rejected): {m.name!r}"
            )
        if not (m.isfile() or m.isdir()):
            raise IntakeFetchError(
                f"Archive contains a special file (rejected): {m.name!r}"
            )
        # Duplicate member paths and case/Unicode-only variants of them —
        # reject outright; the same (normalized) path declared as both file
        # and dir is a collision, whatever the member ordering.
        if key in files or key in dirs:
            if (key in files and m.isdir()) or (key in dirs and m.isfile()):
                raise IntakeFetchError(
                    f"File/directory collision (rejected): {m.name!r} is "
                    "declared as both a file and a directory."
                )
            raise IntakeFetchError(
                f"Duplicate archive member path (rejected; collisions are "
                f"matched case-insensitively): {m.name!r}"
            )
        # No member may live underneath a path already declared as a file
        # (e.g. "demo" file seen before "demo/x", or "DEMO" before "demo/x").
        parent = PurePosixPath(name).parent
        while parent.parts:
            parent_str = parent.as_posix()
            if _collision_key(parent_str) in files:
                raise IntakeFetchError(
                    f"File/directory collision (rejected): {parent_str!r} is "
                    f"a file but {m.name!r} requires it to be a directory."
                )
            parent = parent.parent
        if m.isfile():
            # Children seen earlier already imply this path is a directory
            # (e.g. "demo/x" seen before a file entry named "demo"/"DEMO").
            if key in implied_dirs:
                raise IntakeFetchError(
                    f"File/directory collision (rejected): {m.name!r} is "
                    "declared as a file but earlier entries require it to be "
                    "a directory."
                )
            files.add(key)
        else:
            dirs.add(key)
        for ancestor in PurePosixPath(name).parents:
            if ancestor.parts:
                implied_dirs.add(_collision_key(ancestor.as_posix()))
        if m.isfile():
            if m.size < 0:
                raise IntakeFetchError(f"Negative file size for {m.name!r}.")
            file_count += 1
            total_size += m.size
            if file_count > caps.max_file_count:
                raise IntakeFetchError(
                    f"Archive exceeds the file-count cap ({caps.max_file_count})."
                )
            if total_size > caps.max_extracted_bytes:
                raise IntakeFetchError(
                    f"Archive exceeds the extracted-size cap "
                    f"({caps.max_extracted_bytes} bytes)."
                )
    return members


def safe_extract_tar_gz(
    archive_path: Path,
    dest_dir: Path,
    caps: ArchiveCaps | None = None,
) -> int:
    """
    Safely extract a .tar.gz archive into ``dest_dir``.

    ``dest_dir`` must NOT already exist: this function only ever creates —
    and on failure removes — a directory it created itself during this
    call, so a rejected or invalid archive can never delete or modify
    pre-existing destination content.

    Returns the number of files written. Raises IntakeFetchError on any
    violation; the partial (self-created) extraction is removed before
    raising.
    """
    caps = caps or ArchiveCaps()

    # Reject a pre-existing destination BEFORE reading the archive or
    # writing/deleting anything — cleanup below may then only ever touch a
    # directory this call created itself.
    if dest_dir.exists():
        raise IntakeFetchError(
            f"Destination directory already exists; refusing to extract into "
            f"pre-existing content (nothing was read, written, or deleted): "
            f"{dest_dir}"
        )

    archive_size = archive_path.stat().st_size
    if archive_size > caps.max_archive_bytes:
        raise IntakeFetchError(
            f"Archive exceeds the archive-size cap ({caps.max_archive_bytes} bytes)."
        )

    written = 0
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getmembers()
            # Vet EVERY member before a single byte is written anywhere.
            _vet_members(members, caps)
            dest_dir.mkdir(parents=True, exist_ok=True)
            for m in members:
                target = dest_dir / m.name
                # Final containment check on the resolved destination.
                resolved = target.resolve()
                if not resolved.is_relative_to(dest_dir.resolve()):
                    raise IntakeFetchError(
                        f"Member escapes destination directory: {m.name!r}"
                    )
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    raise IntakeFetchError(f"Cannot read archive member: {m.name!r}")
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, length=65536)
                written += 1
    except (tarfile.TarError, OSError, EOFError) as exc:
        _cleanup_partial(dest_dir)
        raise IntakeFetchError(f"Archive extraction failed: {exc}") from exc
    except IntakeFetchError:
        _cleanup_partial(dest_dir)
        raise
    return written


def _cleanup_partial(dest_dir: Path) -> None:
    """Remove a partially-extracted directory (best effort).

    Only ever called for a destination this call created itself — a
    pre-existing destination is rejected before any work starts, so this
    can never delete unrelated pre-existing content.
    """
    try:
        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
            logger.warning("Cleaned up partial extraction at %s", dest_dir)
    except OSError:  # pragma: no cover — best effort
        pass


def hash_tree(root: Path) -> str:
    """
    Deterministic SHA-256 over the content tree under ``root``.

    Manifest format (sorted by relative POSIX path)::

        sha256( b"".join( f"{relpath}\\0{file_sha256}\\n" ) )

    Uses lstat semantics — symlinks and special files are rejected rather
    than followed, so hashing cannot be tricked into reading outside root.
    Directories contribute only via their files.

    No-follow proof (L8): traversal enumerates ``root.rglob("*")``, which
    does not descend into symlinked directories on the supported runtimes
    (verified empirically on CPython 3.12/Windows: a symlinked directory
    is yielded as an entry but its targets' children are not). Even on a
    hypothetical runtime that did descend, ``sorted()`` orders a symlink
    entry strictly before its own children (a parent is always a string
    prefix of its children), so the ``is_symlink()`` rejection below fires
    before any child path could be hashed. A focused regression test pins
    both properties.
    """
    if not root.is_dir():
        raise IntakeFetchError(f"Cannot hash missing directory: {root}")

    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if len(rel) > 240:
            raise IntakeFetchError(f"Path too long in tree hash: {rel!r}")
        if path.is_symlink():
            raise IntakeFetchError(f"Symlink found in tree (rejected): {rel!r}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise IntakeFetchError(f"Special file found in tree (rejected): {rel!r}")
        entries.append(f"{rel}\0{hash_file_sha256(path)}\n")

    digest = hashlib.sha256("".join(entries).encode("utf-8")).hexdigest()
    return digest


def tree_size_bytes(root: Path) -> int:
    """Total size in bytes of regular files under root (0 if missing).

    No-follow proof (L8): symlinked directories are not descended by
    ``rglob`` on the supported runtimes (verified on CPython 3.12/Windows)
    and the ``not path.is_symlink()`` guard excludes link entries
    themselves, so sizes of files outside ``root`` reached through a link
    are never counted. A focused regression test pins this behavior — if a
    future runtime changes ``rglob`` recursion semantics, that test fails
    loudly rather than silently counting outside bytes.
    """
    total = 0
    if not root.is_dir():
        return 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
    return total
