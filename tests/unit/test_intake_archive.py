"""
Unit tests for factory.intake.archive — safe extraction (traversal,
absolute/drive/UNC paths, symlink/hardlink/special-file rejection, caps,
partial-extraction cleanup) and deterministic tree hashing.

All archives are constructed locally in pytest temp dirs; no network and
no real third-party content is involved.
"""

from __future__ import annotations

import gzip
import io
import os
import tarfile
from pathlib import Path

import pytest

from factory.errors import IntakeFetchError
from factory.intake.archive import (
    ArchiveCaps,
    hash_file_sha256,
    hash_tree,
    safe_extract_tar_gz,
    tree_size_bytes,
)


# ---------------------------------------------------------------------------
# Helpers — build tar.gz fixtures programmatically
# ---------------------------------------------------------------------------

def _tar_bytes(members: list[tuple[str, tarfile.TarInfo, bytes | None]]) -> bytes:
    """Build an in-memory tar.gz from (name, TarInfo, data|None) tuples."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for _name, info, data in members:
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return buf.getvalue()


def _file_info(name: str, *, mode: int = 0o644) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.mode = mode
    return info


def _write_archive(tmp_path: Path, payload: bytes, filename: str = "a.tar.gz") -> Path:
    p = tmp_path / filename
    p.write_bytes(payload)
    return p


def _dir_info(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info


def _good_skill_tree() -> bytes:
    return _tar_bytes([
        ("demo-skill", _dir_info("demo-skill"), None),
        ("demo-skill/SKILL.md", _file_info("demo-skill/SKILL.md"),
         b"# Demo Skill\nYou are a helpful assistant.\n"),
        ("demo-skill/reference/notes.md", _file_info("demo-skill/reference/notes.md"),
         b"Reference material.\n"),
    ])


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestSafeExtractionHappyPath:
    def test_extracts_files_and_reports_count(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, _good_skill_tree())
        dest = tmp_path / "out"
        written = safe_extract_tar_gz(archive, dest)
        assert written == 2
        assert (dest / "demo-skill" / "SKILL.md").read_bytes().startswith(b"# Demo")
        assert (dest / "demo-skill" / "reference" / "notes.md").is_file()

    def test_tree_hash_is_deterministic_across_rebuilds(self, tmp_path: Path) -> None:
        dest1, dest2 = tmp_path / "d1", tmp_path / "d2"
        safe_extract_tar_gz(_write_archive(tmp_path, _good_skill_tree()), dest1)
        # Rebuild the same tree in a *different* tar ordering.
        reordered = _tar_bytes([
            ("demo-skill/reference/notes.md", _file_info("demo-skill/reference/notes.md"),
             b"Reference material.\n"),
            ("demo-skill", _dir_info("demo-skill"), None),
            ("demo-skill/SKILL.md", _file_info("demo-skill/SKILL.md"),
             b"# Demo Skill\nYou are a helpful assistant.\n"),
        ])
        safe_extract_tar_gz(_write_archive(tmp_path, reordered, "b.tar.gz"), dest2)
        assert hash_tree(dest1) == hash_tree(dest2)

    def test_tree_hash_changes_on_content_tamper(self, tmp_path: Path) -> None:
        dest = tmp_path / "out"
        safe_extract_tar_gz(_write_archive(tmp_path, _good_skill_tree()), dest)
        before = hash_tree(dest)
        (dest / "demo-skill" / "SKILL.md").write_bytes(b"# Tampered\n")
        assert hash_tree(dest) != before

    def test_hash_file_sha256_matches_hashlib(self, tmp_path: Path) -> None:
        import hashlib
        f = tmp_path / "x.bin"
        f.write_bytes(b"abc123")
        assert hash_file_sha256(f) == hashlib.sha256(b"abc123").hexdigest()

    def test_tree_size_bytes(self, tmp_path: Path) -> None:
        dest = tmp_path / "out"
        safe_extract_tar_gz(_write_archive(tmp_path, _good_skill_tree()), dest)
        assert tree_size_bytes(dest) == len(
            b"# Demo Skill\nYou are a helpful assistant.\n"
        ) + len(b"Reference material.\n")
        assert tree_size_bytes(tmp_path / "missing") == 0


# ---------------------------------------------------------------------------
# Path-safety rejections — nothing is written outside dest; dest cleaned up
# ---------------------------------------------------------------------------

class TestUnsafePathRejection:
    def _assert_rejected_and_cleaned(self, tmp_path: Path, payload: bytes) -> None:
        archive = _write_archive(tmp_path, payload)
        dest = tmp_path / "out"
        with pytest.raises(IntakeFetchError):
            safe_extract_tar_gz(archive, dest)
        # Partial extraction must be cleaned up.
        assert not dest.exists()

    def test_rejects_parent_traversal(self, tmp_path: Path) -> None:
        self._assert_rejected_and_cleaned(tmp_path, _tar_bytes([
            ("../evil.txt", _file_info("../evil.txt"), b"escape"),
        ]))

    def test_rejects_nested_traversal(self, tmp_path: Path) -> None:
        self._assert_rejected_and_cleaned(tmp_path, _tar_bytes([
            ("demo/../../evil.txt", _file_info("demo/../../evil.txt"), b"escape"),
        ]))

    def test_rejects_absolute_posix_path(self, tmp_path: Path) -> None:
        self._assert_rejected_and_cleaned(tmp_path, _tar_bytes([
            ("/etc/evil.conf", _file_info("/etc/evil.conf"), b"escape"),
        ]))

    def test_rejects_windows_drive_path(self, tmp_path: Path) -> None:
        self._assert_rejected_and_cleaned(tmp_path, _tar_bytes([
            ("C:/Windows/evil.txt", _file_info("C:/Windows/evil.txt"), b"escape"),
        ]))

    def test_rejects_unc_style_path(self, tmp_path: Path) -> None:
        self._assert_rejected_and_cleaned(tmp_path, _tar_bytes([
            ("//server/share/evil.txt", _file_info("//server/share/evil.txt"), b"x"),
        ]))

    def test_rejects_backslash_path(self, tmp_path: Path) -> None:
        self._assert_rejected_and_cleaned(tmp_path, _tar_bytes([
            ("..\\evil.txt", _file_info("..\\evil.txt"), b"x"),
        ]))

    def test_no_traversal_file_written_outside_dest(self, tmp_path: Path) -> None:
        payload = _tar_bytes([
            ("../evil.txt", _file_info("../evil.txt"), b"escape"),
        ])
        with pytest.raises(IntakeFetchError):
            safe_extract_tar_gz(_write_archive(tmp_path, payload), tmp_path / "out")
        assert not (tmp_path / "evil.txt").exists()


# ---------------------------------------------------------------------------
# Link / special-file rejections
# ---------------------------------------------------------------------------

class TestLinkAndSpecialFileRejection:
    def _assert_rejected_and_cleaned(self, tmp_path: Path, payload: bytes) -> None:
        archive = _write_archive(tmp_path, payload)
        dest = tmp_path / "out"
        with pytest.raises(IntakeFetchError):
            safe_extract_tar_gz(archive, dest)
        assert not dest.exists()

    def test_rejects_symlink(self, tmp_path: Path) -> None:
        info = tarfile.TarInfo("demo/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        self._assert_rejected_and_cleaned(tmp_path, _tar_bytes([
            ("demo/link", info, None),
        ]))

    def test_rejects_hardlink(self, tmp_path: Path) -> None:
        info = tarfile.TarInfo("demo/hard")
        info.type = tarfile.LNKTYPE
        info.linkname = "demo/SKILL.md"
        self._assert_rejected_and_cleaned(tmp_path, _tar_bytes([
            ("demo/hard", info, None),
        ]))

    def test_rejects_char_device(self, tmp_path: Path) -> None:
        info = tarfile.TarInfo("demo/dev")
        info.type = tarfile.CHRTYPE
        self._assert_rejected_and_cleaned(tmp_path, _tar_bytes([
            ("demo/dev", info, None),
        ]))

    def test_rejects_fifo(self, tmp_path: Path) -> None:
        info = tarfile.TarInfo("demo/fifo")
        info.type = tarfile.FIFOTYPE
        self._assert_rejected_and_cleaned(tmp_path, _tar_bytes([
            ("demo/fifo", info, None),
        ]))


# ---------------------------------------------------------------------------
# Windows-unsafe component names (Finding 1) — rejected before extraction
# ---------------------------------------------------------------------------

class TestWindowsUnsafeNameRejection:
    def _reject(
        self,
        tmp_path: Path,
        members: list[tuple[str, tarfile.TarInfo, bytes | None]],
    ) -> str:
        """Vet-reject the archive and assert nothing was written to dest."""
        archive = _write_archive(tmp_path, _tar_bytes(members))
        dest = tmp_path / "out"
        with pytest.raises(IntakeFetchError) as excinfo:
            safe_extract_tar_gz(archive, dest)
        assert not dest.exists()  # vet-before-write: dest never created
        return str(excinfo.value)

    def test_ntfs_alternate_data_stream_name_rejected(self, tmp_path: Path) -> None:
        # "SKILL.md:payload" would be an ADS on NTFS — not an ordinary file.
        name = "demo-skill/SKILL.md:payload"
        msg = self._reject(tmp_path, [(
            name, _file_info(name), b"hidden stream",
        )])
        assert "Windows-forbidden character" in msg
        assert ":" in msg

    @pytest.mark.parametrize("ch", ["<", ">", '"', "|", "?", "*"])
    def test_forbidden_windows_characters_rejected(
        self, tmp_path: Path, ch: str,
    ) -> None:
        name = f"bad{ch}name.txt"
        msg = self._reject(tmp_path, [(
            name, _file_info(name), b"x",
        )])
        assert "Windows-forbidden character" in msg

    @pytest.mark.parametrize("name", [
        "CON", "con", "NUL.txt", "COM1.md", "aux", "LPT9.tar.gz",
        "demo/PRN.cfg",
    ])
    def test_reserved_device_names_rejected(
        self, tmp_path: Path, name: str,
    ) -> None:
        msg = self._reject(tmp_path, [(name, _file_info(name), b"x")])
        assert "reserved Windows device name" in msg

    @pytest.mark.parametrize("name", [
        "evil.", "evil ", "demo/evil.", "demo/evil ",
    ])
    def test_trailing_period_or_space_rejected(
        self, tmp_path: Path, name: str,
    ) -> None:
        msg = self._reject(tmp_path, [(name, _file_info(name), b"x")])
        assert "ends with a space or period" in msg

    def test_reserved_check_is_exact_base_not_substring(
        self, tmp_path: Path,
    ) -> None:
        # "console.md" contains "CON" but is a legitimate filename —
        # the reserved-name check must stay exact to avoid over-blocking.
        payload = _tar_bytes([
            ("docs/console.md", _file_info("docs/console.md"), b"ok\n"),
        ])
        dest = tmp_path / "out"
        assert safe_extract_tar_gz(_write_archive(tmp_path, payload), dest) == 1
        assert (dest / "docs" / "console.md").read_bytes() == b"ok\n"

    def test_traversal_error_not_mislabelled_as_windows_name(
        self, tmp_path: Path,
    ) -> None:
        # Regression: a '..' component must fail for traversal/structure,
        # never with a misleading "trailing space/period" Windows detail.
        msg = self._reject(tmp_path, [(
            "../evil.txt", _file_info("../evil.txt"), b"escape",
        )])
        assert "Unsafe archive member path rejected" in msg
        assert "space or period" not in msg
        assert "reserved Windows device" not in msg


# ---------------------------------------------------------------------------
# Duplicates and file/directory collisions — order independent, pre-write
# ---------------------------------------------------------------------------

class TestDuplicateAndCollisionRejection:
    def _reject(
        self,
        tmp_path: Path,
        members: list[tuple[str, tarfile.TarInfo, bytes | None]],
        match: str,
    ) -> None:
        archive = _write_archive(tmp_path, _tar_bytes(members))
        dest = tmp_path / "out"
        with pytest.raises(IntakeFetchError, match=match):
            safe_extract_tar_gz(archive, dest)
        assert not dest.exists()

    def test_duplicate_file_paths_rejected(self, tmp_path: Path) -> None:
        self._reject(tmp_path, [
            ("dup.txt", _file_info("dup.txt"), b"first"),
            ("dup.txt", _file_info("dup.txt"), b"second"),
        ], "Duplicate archive member path")

    def test_duplicate_directory_entries_rejected(self, tmp_path: Path) -> None:
        self._reject(tmp_path, [
            ("dd", _dir_info("dd"), None),
            ("dd", _dir_info("dd"), None),
        ], "Duplicate archive member path")

    def test_same_path_as_file_and_directory_rejected(
        self, tmp_path: Path,
    ) -> None:
        self._reject(tmp_path, [
            ("dd", _dir_info("dd"), None),
            ("dd", _file_info("dd"), b"x"),
        ], "File/directory collision")

    def test_file_then_child_collision_rejected(self, tmp_path: Path) -> None:
        # A file entry seen first forbids anything underneath it later.
        self._reject(tmp_path, [
            ("demo", _file_info("demo"), b"a file, not a dir"),
            ("demo/x.txt", _file_info("demo/x.txt"), b"x"),
        ], "File/directory collision")

    def test_child_then_file_collision_rejected(self, tmp_path: Path) -> None:
        # ...and the reverse order must be caught too (order independent).
        self._reject(tmp_path, [
            ("demo/x.txt", _file_info("demo/x.txt"), b"x"),
            ("demo", _file_info("demo"), b"a file, not a dir"),
        ], "File/directory collision")

    def test_rejection_leaves_only_the_archive_on_disk(
        self, tmp_path: Path,
    ) -> None:
        # True pre-extraction rejection: vetting runs before any write, so
        # no dest dir and no stray files appear anywhere in the workspace.
        payload = _tar_bytes([
            ("demo", _file_info("demo"), b"x"),
            ("demo/x.txt", _file_info("demo/x.txt"), b"x"),
        ])
        archive = _write_archive(tmp_path, payload)
        with pytest.raises(IntakeFetchError):
            safe_extract_tar_gz(archive, tmp_path / "out")
        assert {p.name for p in tmp_path.iterdir()} == {archive.name}


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

class TestCaps:
    def test_file_count_cap(self, tmp_path: Path) -> None:
        payload = _tar_bytes([
            (f"f{i}.txt", _file_info(f"f{i}.txt"), b"x") for i in range(3)
        ])
        caps = ArchiveCaps(max_file_count=2)
        with pytest.raises(IntakeFetchError, match="file-count"):
            safe_extract_tar_gz(_write_archive(tmp_path, payload), tmp_path / "out",
                                caps)

    def test_extracted_size_cap(self, tmp_path: Path) -> None:
        payload = _tar_bytes([
            ("big.bin", _file_info("big.bin"), b"x" * 100),
        ])
        caps = ArchiveCaps(max_extracted_bytes=50)
        with pytest.raises(IntakeFetchError, match="extracted-size"):
            safe_extract_tar_gz(_write_archive(tmp_path, payload), tmp_path / "out",
                                caps)

    def test_archive_size_cap(self, tmp_path: Path) -> None:
        payload = _good_skill_tree()  # gzipped, > 100 bytes
        caps = ArchiveCaps(max_archive_bytes=100)
        with pytest.raises(IntakeFetchError, match="archive-size"):
            safe_extract_tar_gz(_write_archive(tmp_path, payload), tmp_path / "out",
                                caps)

    def test_path_length_cap(self, tmp_path: Path) -> None:
        deep = "d/" * 100 + "f.txt"
        payload = _tar_bytes([(deep, _file_info(deep), b"x")])
        caps = ArchiveCaps(max_path_length=50)
        with pytest.raises(IntakeFetchError, match="path-length"):
            safe_extract_tar_gz(_write_archive(tmp_path, payload), tmp_path / "out",
                                caps)


# ---------------------------------------------------------------------------
# Corrupt archive → cleanup
# ---------------------------------------------------------------------------

class TestCorruptArchive:
    def test_non_gzip_bytes_fail_and_clean_dest(self, tmp_path: Path) -> None:
        archive = tmp_path / "corrupt.tar.gz"
        archive.write_bytes(b"this is not a gzip stream at all")
        dest = tmp_path / "out"
        with pytest.raises(IntakeFetchError):
            safe_extract_tar_gz(archive, dest)
        assert not dest.exists()

    def test_truncated_gzip_fails_and_cleans_dest(self, tmp_path: Path) -> None:
        good = _good_skill_tree()
        archive = tmp_path / "trunc.tar.gz"
        archive.write_bytes(good[: len(good) // 2])
        dest = tmp_path / "out"
        with pytest.raises(IntakeFetchError):
            safe_extract_tar_gz(archive, dest)
        assert not dest.exists()

    def test_gzip_bomb_header_with_garbage_body(self, tmp_path: Path) -> None:
        # Valid gzip header wrapping non-tar content.
        archive = tmp_path / "bomb.tar.gz"
        archive.write_bytes(gzip.compress(b"not a tarfile"))
        with pytest.raises(IntakeFetchError):
            safe_extract_tar_gz(archive, tmp_path / "out")


# ---------------------------------------------------------------------------
# hash_tree fail-closed on links
# ---------------------------------------------------------------------------

class TestHashTreeLinkRejection:
    def test_symlink_in_tree_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        root.mkdir()
        (root / "real.txt").write_bytes(b"data")
        try:
            os.symlink(root / "real.txt", root / "link.txt")
        except OSError:
            pytest.skip(
                "symlink creation not permitted on this host "
                "(requires Windows developer mode or elevation)"
            )
        with pytest.raises(IntakeFetchError, match="[Ss]ymlink"):
            hash_tree(root)

    def test_missing_root_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(IntakeFetchError):
            hash_tree(tmp_path / "nope")


# ---------------------------------------------------------------------------
# Case-insensitive / Unicode-normalized collision rejection (M3)
# ---------------------------------------------------------------------------

class TestCaseInsensitiveAndUnicodeCollisions:
    """Collisions are matched on NFC + casefold keys (Windows semantics),
    while the original member names are kept for extraction."""

    def _reject(
        self,
        tmp_path: Path,
        members: list[tuple[str, tarfile.TarInfo, bytes | None]],
        match: str,
    ) -> None:
        archive = _write_archive(tmp_path, _tar_bytes(members))
        dest = tmp_path / "out"
        with pytest.raises(IntakeFetchError, match=match):
            safe_extract_tar_gz(archive, dest)
        assert not dest.exists()

    @pytest.mark.parametrize("reverse", [False, True])
    def test_case_only_file_collision_both_orders(
        self, tmp_path: Path, reverse: bool,
    ) -> None:
        members = [
            ("docs/readme.md", _file_info("docs/readme.md"), b"a"),
            ("DOCS/README.MD", _file_info("DOCS/README.MD"), b"b"),
        ]
        if reverse:
            members.reverse()
        self._reject(tmp_path, members, "Duplicate archive member path")

    @pytest.mark.parametrize("reverse", [False, True])
    def test_case_only_directory_collision_both_orders(
        self, tmp_path: Path, reverse: bool,
    ) -> None:
        members = [
            ("Docs", _dir_info("Docs"), None),
            ("DOCS", _dir_info("DOCS"), None),
        ]
        if reverse:
            members.reverse()
        self._reject(tmp_path, members, "Duplicate archive member path")

    @pytest.mark.parametrize("reverse", [False, True])
    def test_case_only_file_directory_collision_both_orders(
        self, tmp_path: Path, reverse: bool,
    ) -> None:
        members = [
            ("docs", _file_info("docs"), b"a file, not a dir"),
            ("DOCS", _dir_info("DOCS"), None),
        ]
        if reverse:
            members.reverse()
        self._reject(tmp_path, members, "File/directory collision")

    @pytest.mark.parametrize("reverse", [False, True])
    def test_implied_parent_collision_case_only_both_orders(
        self, tmp_path: Path, reverse: bool,
    ) -> None:
        # "docs/readme.md" implies directory "docs"; a file entry "DOCS"
        # claims that implied directory — in either member order.
        members = [
            ("docs/readme.md", _file_info("docs/readme.md"), b"a"),
            ("DOCS", _file_info("DOCS"), b"b"),
        ]
        if reverse:
            members.reverse()
        self._reject(tmp_path, members, "File/directory collision")

    @pytest.mark.parametrize("reverse", [False, True])
    def test_unicode_normalization_collision_both_orders(
        self, tmp_path: Path, reverse: bool,
    ) -> None:
        # NFC "caf\u00e9" vs NFD "cafe\u0301" — one visual path on
        # normalizing filesystems; must collide after normalization.
        members = [
            ("caf\u00e9/notes.md", _file_info("caf\u00e9/notes.md"), b"a"),
            ("cafe\u0301/notes.md", _file_info("cafe\u0301/notes.md"), b"b"),
        ]
        if reverse:
            members.reverse()
        self._reject(tmp_path, members, "Duplicate archive member path")

    def test_legitimate_case_distinct_dirs_still_extract(self, tmp_path: Path) -> None:
        # Two genuinely different directories remain allowed (no false
        # positive on exact, non-colliding trees).
        payload = _tar_bytes([
            ("docs", _dir_info("docs"), None),
            ("docs/a.md", _file_info("docs/a.md"), b"a"),
            ("reference", _dir_info("reference"), None),
            ("reference/b.md", _file_info("reference/b.md"), b"b"),
        ])
        dest = tmp_path / "out"
        assert safe_extract_tar_gz(_write_archive(tmp_path, payload), dest) == 2


# ---------------------------------------------------------------------------
# Pre-existing destination rejection (L6)
# ---------------------------------------------------------------------------

class TestPreExistingDestinationRejected:
    """safe_extract_tar_gz only ever creates/cleans a directory it created
    itself — pre-existing destination content survives every failure."""

    def test_sentinel_survives_failing_archive(self, tmp_path: Path) -> None:
        dest = tmp_path / "out"
        dest.mkdir()
        sentinel = dest / "sentinel.txt"
        sentinel.write_bytes(b"pre-existing content - must survive")

        archive = _write_archive(tmp_path, b"this is not a gzip stream at all")
        with pytest.raises(IntakeFetchError):
            safe_extract_tar_gz(archive, dest)

        # Rejected before any read/write/delete: sentinel byte-identical and
        # no extraction output mixed into the pre-existing directory.
        assert sentinel.read_bytes() == b"pre-existing content - must survive"
        assert {p.name for p in dest.iterdir()} == {"sentinel.txt"}

    def test_valid_archive_also_rejected_into_existing_destination(
        self, tmp_path: Path,
    ) -> None:
        dest = tmp_path / "out"
        dest.mkdir()
        sentinel = dest / "sentinel.txt"
        sentinel.write_bytes(b"keep")

        archive = _write_archive(tmp_path, _good_skill_tree())
        with pytest.raises(IntakeFetchError, match="already exists"):
            safe_extract_tar_gz(archive, dest)

        assert {p.name for p in dest.iterdir()} == {"sentinel.txt"}

    def test_fresh_destination_still_works(self, tmp_path: Path) -> None:
        dest = tmp_path / "out"
        archive = _write_archive(tmp_path, _good_skill_tree())
        assert safe_extract_tar_gz(archive, dest) == 2
        assert (dest / "demo-skill" / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# Traversal never follows symlinked directories (L8: proof + regression pin)
# ---------------------------------------------------------------------------

class TestTraversalNoFollow:
    def _tree_with_outside_dir_link(self, tmp_path: Path) -> tuple[Path, Path]:
        root = tmp_path / "tree"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "inside.txt").write_bytes(b"inside")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"outside-secret-bytes")
        try:
            os.symlink(outside, root / "link")
        except OSError:
            pytest.skip(
                "symlink creation not permitted on this host "
                "(requires Windows developer mode or elevation)"
            )
        return root, outside

    def test_hash_tree_rejects_symlinked_directory(self, tmp_path: Path) -> None:
        root, _outside = self._tree_with_outside_dir_link(tmp_path)
        with pytest.raises(IntakeFetchError, match="[Ss]ymlink"):
            hash_tree(root)

    def test_tree_size_bytes_excludes_bytes_outside_root(self, tmp_path: Path) -> None:
        root, _outside = self._tree_with_outside_dir_link(tmp_path)
        inside = len(b"inside")
        secret = len(b"outside-secret-bytes")
        # Symlinked directory content is never counted: if rglob recursion
        # semantics ever changed to follow links, this assertion fails
        # loudly instead of silently counting outside bytes.
        assert tree_size_bytes(root) == inside
        assert tree_size_bytes(root) != inside + secret
