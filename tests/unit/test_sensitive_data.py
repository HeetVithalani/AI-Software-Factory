"""
tests/unit/test_sensitive_data.py
==================================
Deterministic unit tests for SensitiveDataDetector (SEC-005).

Covers Windows-aware path normalisation: trailing dots/spaces (Win32
strips them on open), NTFS alternate data streams, duplicate
separators, case variants, traversal spellings, plus content-pattern
detection and benign false-positive resistance.

No network, no models, no filesystem access (paths are checked as
strings — check_file is not exercised here).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.permissions.sensitive_data import SensitiveDataDetector


@pytest.fixture
def detector() -> SensitiveDataDetector:
    return SensitiveDataDetector()


class TestPathDetection:
    @pytest.mark.parametrize("raw", [
        ".env",
        ".ENV",
        ".env.local",
        ".env.",                                  # Win32 strips trailing dot
        ".env   ",                                # Win32 strips trailing spaces
        "config/.env:$DATA",                      # NTFS alternate data stream
        "C:\\Users\\dev\\.env.production",
        "app\\..\\.env",                          # traversal spelling
        "x//secrets//key.pem",                    # duplicate separators
    ])
    def test_sensitive_names_detected(self, detector, raw):
        report = detector.check_path(Path(raw))
        assert report.is_sensitive, raw
        assert report.severity in ("high", "critical")

    @pytest.mark.parametrize("raw", [
        "C:\\Users\\dev\\.ssh\\id_rsa",
        "home/user/.ssh/authorized_keys",
        "repo/.aws/credentials",
        "proj\\SECRETS\\db.json",                 # case-variant segment
        "project/../.gnupg/pubring.kbx",
        "backup/prod-settings.yaml",              # prod.* pattern
        "dump/db-backup.sql.gz",                  # dump pattern
    ])
    def test_sensitive_segments_detected(self, detector, raw):
        report = detector.check_path(Path(raw))
        assert report.is_sensitive, raw

    @pytest.mark.parametrize("raw", [
        "src/main.py",
        "notes.txt",
        "docs/readme.md",
        "app/config/settings.py",
        "C:\\temp\\ordinary_file.bin",
        "tests/test_example.py",
        ".gitignore",
        "token.txt",                              # no matching secret pattern
    ])
    def test_benign_paths_not_flagged(self, detector, raw):
        report = detector.check_path(Path(raw))
        assert not report.is_sensitive, (raw, report.reasons)

    def test_trailing_dot_env_resolves_to_env_pattern(self, detector):
        """'.env.' must match the exact-name '^\\.env$' pattern after
        normalisation — the Win32-opened file IS '.env'."""
        report = detector.check_path(Path("secrets_dir/.env."))
        assert report.is_sensitive
        # and the raw (unnormalised) name would NOT have matched alone
        assert not Path(".env.").name == ".env"  # documents why normalise exists


class TestContentDetection:
    def test_pem_private_key_detected(self, detector):
        report = detector.check_content(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n"
        )
        assert report.is_sensitive
        assert report.severity == "critical"

    def test_connection_url_detected(self, detector):
        report = detector.check_content(
            "DATABASE_URL = 'postgres://user:pass@host:5432/db'"
        )
        assert report.is_sensitive

    def test_password_assignment_detected(self, detector):
        report = detector.check_content('password = "hunter2hunter2"')
        assert report.is_sensitive

    def test_benign_content_not_flagged(self, detector):
        report = detector.check_content(
            "def add(a, b):\n    return a + b\n"
            "# token rotation is documented in docs/security.md\n"
        )
        assert not report.is_sensitive, report.reasons

    def test_content_inspection_is_bounded(self, detector):
        """Only the first max_bytes are inspected (documented bound)."""
        filler = "x" * 70_000
        report = detector.check_content(
            filler + "\npassword = \"hunter2hunter2\"", max_bytes=65_536
        )
        assert not report.is_sensitive  # secret lies beyond the bound
