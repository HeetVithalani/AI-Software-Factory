"""
factory.permissions.sensitive_data
====================================
Sensitive data detection — pattern-based, deterministic.

Detects file paths and content patterns that indicate sensitive material.
The detector NEVER relies on an LLM judgment — it uses explicit patterns
and heuristics only.

Sensitive files include at minimum:
- .env files and variants
- Private keys and certificates
- Cloud credentials (AWS, GCP, Azure)
- Database dumps
- Production configuration files
- Authentication secrets
- Backup archives containing sensitive information
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sensitive path patterns
# ---------------------------------------------------------------------------

# File name / extension patterns that always indicate sensitive content
_SENSITIVE_FILENAME_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\.env$", re.IGNORECASE),
    re.compile(r"^\.env\..+$", re.IGNORECASE),          # .env.production etc.
    re.compile(r".*\.pem$", re.IGNORECASE),
    re.compile(r".*\.key$", re.IGNORECASE),
    re.compile(r".*\.pfx$", re.IGNORECASE),
    re.compile(r".*\.p12$", re.IGNORECASE),
    re.compile(r".*\.crt$", re.IGNORECASE),
    re.compile(r".*\.cer$", re.IGNORECASE),
    re.compile(r"id_rsa$", re.IGNORECASE),
    re.compile(r"id_dsa$", re.IGNORECASE),
    re.compile(r"id_ecdsa$", re.IGNORECASE),
    re.compile(r"id_ed25519$", re.IGNORECASE),
    re.compile(r".*credentials.*", re.IGNORECASE),
    re.compile(r".*secrets?.*\.(json|yaml|yml|toml)$", re.IGNORECASE),
    re.compile(r".*token.*\.(json|yaml|yml|toml)$", re.IGNORECASE),
    re.compile(r".*keyfile.*", re.IGNORECASE),
    re.compile(r"service[-_]?account.*\.json$", re.IGNORECASE),
    re.compile(r".*\.sql\.gz$", re.IGNORECASE),           # DB dumps
    re.compile(r".*dump.*\.sql$", re.IGNORECASE),
    re.compile(r".*backup.*\.(tar|zip|gz|bz2|7z)$", re.IGNORECASE),
    re.compile(r".*prod(?:uction)?.*\.(yaml|yml|toml|json|conf|ini)$", re.IGNORECASE),
    re.compile(r"\.aws/credentials$", re.IGNORECASE),
    re.compile(r"\.aws/config$", re.IGNORECASE),
    re.compile(r"gcloud.*\.json$", re.IGNORECASE),
    re.compile(r"kubeconfig.*", re.IGNORECASE),
    re.compile(r".*htpasswd.*", re.IGNORECASE),
]

# Path segment patterns (any component of the path)
_SENSITIVE_PATH_SEGMENTS: list[str] = [
    ".ssh",
    ".gnupg",
    ".aws",
    ".gcloud",
    ".azure",
    "secrets",
    "private_keys",
    "certs",
    "certificates",
]

# Content patterns that indicate a file contains secrets
_SENSITIVE_CONTENT_PATTERNS: list[re.Pattern] = [
    re.compile(r"BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY", re.IGNORECASE),
    re.compile(r"BEGIN CERTIFICATE", re.IGNORECASE),
    re.compile(r"(?:password|passwd|secret|token|api[_-]?key)\s*=\s*['\"].{4,}['\"]",
               re.IGNORECASE),
    re.compile(r"AWS_SECRET_ACCESS_KEY\s*=", re.IGNORECASE),
    re.compile(r"GOOGLE_APPLICATION_CREDENTIALS\s*=", re.IGNORECASE),
    re.compile(r"AZURE_CLIENT_SECRET\s*=", re.IGNORECASE),
    re.compile(r"DATABASE_URL\s*=\s*['\"]?\w+://", re.IGNORECASE),
    re.compile(r"(?:mysql|postgres|postgresql|mongodb|redis)://[^@\s]+@", re.IGNORECASE),
]


@dataclass
class SensitivityReport:
    """Result of a sensitivity check."""
    is_sensitive: bool = False
    reasons: list[str] = field(default_factory=list)
    # Severity: "low", "medium", "high", "critical"
    severity: str = "safe"

    def add_reason(self, reason: str, severity: str = "high") -> None:
        self.is_sensitive = True
        self.reasons.append(reason)
        # Escalate severity (critical > high > medium > low > safe)
        _order = ["safe", "low", "medium", "high", "critical"]
        if _order.index(severity) > _order.index(self.severity):
            self.severity = severity


def _normalize_path_text(raw: str) -> str:
    """
    Normalise a path string so pattern matching sees the name Win32
    will actually open (SEC-005).

    * backslashes → forward slashes; duplicate separators collapse;
    * NTFS alternate-data-stream suffixes stripped (``.env:$DATA`` →
      ``.env`` — Win32 opens the base stream);
    * trailing dots and spaces stripped per component (Win32 strips
      them on open: ``.env.`` → ``.env``).
    """
    text = raw.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    components: list[str] = []
    for comp in text.split("/"):
        if len(comp) == 2 and comp[1] == ":":
            components.append(comp)          # drive letter, keep as-is
            continue
        if ":" in comp:
            comp = comp.split(":", 1)[0]     # drop ADS/stream suffix
        components.append(comp.rstrip(" .")) # Win32 strips trailing dots/spaces
    return "/".join(components)


class SensitiveDataDetector:
    """
    Deterministic sensitive data detector.

    Uses explicit file name patterns, path segment matching, and content
    regex patterns. No LLM involvement.

    Usage::

        detector = SensitiveDataDetector()
        report = detector.check_path(Path(".env"))
        if report.is_sensitive:
            raise ApprovalRequiredError(...)
    """

    def check_path(self, path: Path) -> SensitivityReport:
        """
        Check whether a filesystem path refers to sensitive material.

        The path is normalised before matching (SEC-005):

        * backslashes become forward slashes, duplicate separators
          collapse;
        * NTFS alternate-data-stream suffixes (``name:stream``) are
          stripped from each component — Win32 opens the base file, and
          an attacker could otherwise hide ``.env`` behind ``.env:$DATA``;
        * trailing dots and spaces are stripped from each component —
          Win32 strips them on open (``.env.`` and ``.env   `` resolve to
          ``.env``), so patterns must see the resolved name.
        """
        report = SensitivityReport()
        path_str = _normalize_path_text(str(path))
        components = path_str.split("/")
        name = components[-1] if components else ""

        # Check filename patterns
        for pattern in _SENSITIVE_FILENAME_PATTERNS:
            if pattern.match(name) or pattern.search(path_str):
                report.add_reason(
                    f"Filename matches sensitive pattern: {pattern.pattern!r}",
                    severity="high",
                )
                break

        # Check path segments
        parts = set(segment for segment in (c.lower() for c in components) if segment)
        for segment in _SENSITIVE_PATH_SEGMENTS:
            if segment.lower() in parts:
                report.add_reason(
                    f"Path contains sensitive segment: {segment!r}",
                    severity="high",
                )

        return report

    def check_content(self, content: str, *, max_bytes: int = 65536) -> SensitivityReport:
        """
        Check whether file content contains sensitive patterns.

        Only inspects the first ``max_bytes`` bytes of content.
        """
        report = SensitivityReport()
        snippet = content[:max_bytes]

        for pattern in _SENSITIVE_CONTENT_PATTERNS:
            if pattern.search(snippet):
                report.add_reason(
                    f"Content matches sensitive pattern: {pattern.pattern!r}",
                    severity="critical",
                )

        return report

    def check_file(self, path: Path, *, read_content: bool = True) -> SensitivityReport:
        """
        Combined path + content check for a real file.

        If read_content=True, reads up to 64KB of file content.
        Returns the most severe SensitivityReport found.
        """
        report = self.check_path(path)

        if read_content and path.is_file():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                content_report = self.check_content(content)
                if content_report.is_sensitive:
                    for reason in content_report.reasons:
                        report.add_reason(reason, severity=content_report.severity)
            except OSError as exc:
                logger.debug("Cannot read %s for sensitivity check: %s", path, exc)

        return report

    def is_sensitive_path(self, path: Path) -> bool:
        """Quick boolean check — True if path is sensitive."""
        return self.check_path(path).is_sensitive
