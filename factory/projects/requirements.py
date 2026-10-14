"""
factory.projects.requirements
==============================
Requirements parsing — reads project requirements from YAML or plain text.

Supports two formats:
1. Structured YAML (requirements.yaml in workspace)
2. Plain text description (fallback, keyword extraction)

Output: Requirements dataclass with functional/non-functional/quality gates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_QUALITY_GATES = {
    "test_coverage_min_pct": 0,   # percent
    "max_security_findings": 0,   # zero critical findings
    "max_failing_tests": 0,
}


@dataclass
class Requirements:
    """
    Structured project requirements.

    Attributes
    ----------
    functional:
        List of functional requirement strings.
    non_functional:
        List of non-functional requirement strings.
    quality_gates:
        Dict of quality thresholds (test coverage, max findings, etc.).
    raw:
        Original raw text or YAML content.
    source:
        Where requirements were loaded from: "yaml", "text", "default".
    """
    functional: list[str] = field(default_factory=list)
    non_functional: list[str] = field(default_factory=list)
    quality_gates: dict[str, Any] = field(
        default_factory=lambda: dict(_DEFAULT_QUALITY_GATES)
    )
    raw: str = ""
    source: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "functional": self.functional,
            "non_functional": self.non_functional,
            "quality_gates": self.quality_gates,
            "source": self.source,
        }


class RequirementsParser:
    """
    Parses project requirements from a workspace file or description string.

    Usage::

        parser = RequirementsParser()

        # From workspace file
        reqs = parser.parse_workspace(Path("/project"))

        # From description text
        reqs = parser.parse_text("Build a REST API with authentication")

        # From dict (already structured)
        reqs = parser.parse_dict({"functional": [...], ...})
    """

    _YAML_FILENAME = "requirements.yaml"
    _ALT_FILENAMES = ["requirements.yml", "REQUIREMENTS.md"]

    def parse_workspace(self, workspace: Path) -> Requirements:
        """
        Try to load requirements from the workspace.

        Looks for requirements.yaml, requirements.yml, or REQUIREMENTS.md.
        Falls back to empty Requirements if none found.
        """
        workspace = workspace.resolve()

        # Try YAML first
        yaml_path = workspace / self._YAML_FILENAME
        if yaml_path.exists():
            return self._parse_yaml_file(yaml_path)

        for alt in self._ALT_FILENAMES:
            alt_path = workspace / alt
            if alt_path.exists():
                if alt_path.suffix.lower() in (".yaml", ".yml"):
                    return self._parse_yaml_file(alt_path)
                else:
                    text = alt_path.read_text(encoding="utf-8", errors="replace")
                    return self.parse_text(text)

        logger.debug(
            "RequirementsParser: no requirements file found in %s.", workspace
        )
        return Requirements(source="default")

    def parse_text(self, text: str) -> Requirements:
        """
        Extract requirements from a plain-text description using keyword matching.

        Heuristics:
        - Lines starting with "-", "*", "•", or numbered lists → functional reqs
        - Lines containing "must", "should", "shall" → functional reqs
        - Lines containing "performance", "security", "scalab" → non-functional
        """
        reqs = Requirements(raw=text, source="text")
        lines = [l.strip() for l in text.splitlines() if l.strip()]

        nf_keywords = {
            "performance", "scalab", "availab", "reliab", "security",
            "maintainab", "usabilit", "latency", "throughput",
        }
        func_patterns = re.compile(
            r"^(?:[-*•]|\d+\.)\s+(.+)$|(?:must|should|shall)\s+(.+)",
            re.IGNORECASE,
        )

        for line in lines:
            lower = line.lower()
            # Non-functional check
            if any(kw in lower for kw in nf_keywords):
                reqs.non_functional.append(line)
                continue
            # Functional check
            m = func_patterns.match(line)
            if m:
                req_text = (m.group(1) or m.group(2) or "").strip()
                if req_text:
                    reqs.functional.append(req_text)

        # If we got nothing meaningful, treat whole text as one functional req
        if not reqs.functional and text.strip():
            reqs.functional.append(text.strip()[:500])

        return reqs

    def parse_dict(self, data: dict[str, Any]) -> Requirements:
        """Parse requirements from a pre-structured dict."""
        reqs = Requirements(source="dict")
        reqs.functional = list(data.get("functional", []))
        reqs.non_functional = list(data.get("non_functional", []))
        gates = dict(_DEFAULT_QUALITY_GATES)
        gates.update(data.get("quality_gates", {}))
        reqs.quality_gates = gates
        return reqs

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_yaml_file(self, path: Path) -> Requirements:
        """Parse a requirements YAML file."""
        try:
            import yaml
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as exc:
            logger.warning(
                "RequirementsParser: failed to parse %s: %s", path, exc
            )
            return Requirements(source="yaml_error")

        if not isinstance(data, dict):
            logger.warning(
                "RequirementsParser: %s did not contain a YAML mapping.", path
            )
            return Requirements(source="yaml_error")

        reqs = Requirements(raw=path.read_text(encoding="utf-8", errors="replace"),
                            source="yaml")
        reqs.functional = [str(r) for r in data.get("functional", [])]
        reqs.non_functional = [str(r) for r in data.get("non_functional", [])]

        gates = dict(_DEFAULT_QUALITY_GATES)
        gates.update(data.get("quality_gates", {}))
        reqs.quality_gates = gates

        logger.info(
            "RequirementsParser: loaded %d functional, %d non-functional from %s.",
            len(reqs.functional), len(reqs.non_functional), path,
        )
        return reqs
