"""
Unit tests for ASCII-safe user-facing output in factory.cli.intake_cmd (M4).

Windows redirection/piping defaults to CP1252 stdout, which cannot encode
decorations like U+2713 (CHECK MARK) or U+2500 (BOX DRAWINGS LIGHT
HORIZONTAL) - printing them raises UnicodeEncodeError and crashes the
command mid-output. Every string literal in the intake CLI must therefore
be ASCII-only so redirected output can never fail to encode.

All tests are pure source analysis - no subprocess, no DB, no network.
"""

from __future__ import annotations

import ast
from pathlib import Path

from factory.cli import intake_cmd


def _module_source() -> str:
    return Path(intake_cmd.__file__).read_text(encoding="utf-8")


def _string_constants(node: ast.AST) -> list[tuple[int, str]]:
    return [
        (child.lineno, child.value)
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


class TestIntakeCliAsciiOutput:
    def test_all_string_literals_are_ascii(self) -> None:
        """Every literal (prints, argparse help, docstrings) is ASCII-safe."""
        tree = ast.parse(_module_source())
        offenders = [
            (lineno, value.encode("ascii", "backslashreplace").decode("ascii"))
            for lineno, value in _string_constants(tree)
            if not value.isascii()
        ]
        assert offenders == [], f"non-ASCII string literals: {offenders}"

    def test_print_literals_encode_under_cp1252(self) -> None:
        """Everything reaching print() must encode under CP1252 (the
        Windows redirected/piped stdout encoding)."""
        tree = ast.parse(_module_source())
        printed: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                printed.extend(_string_constants(node))

        assert printed, "expected print() calls in intake_cmd"
        offenders: list[tuple[int, str]] = []
        for lineno, text in printed:
            try:
                text.encode("cp1252")
            except UnicodeEncodeError as exc:
                offenders.append((lineno, str(exc)))
        assert offenders == [], f"print() literals not CP1252-safe: {offenders}"

    def test_module_source_is_pure_ascii(self) -> None:
        """Whole-module belt-and-braces: comments/docstrings included, so no
        non-ASCII text can creep back into any string a user might see."""
        assert _module_source().isascii()

    def test_ascii_replacements_are_present(self) -> None:
        """The fixed decorations use the chosen ASCII equivalents."""
        source = _module_source()
        assert "[OK]" in source                       # replaced check mark
        assert '"-" * 60' in source                   # replaced box-drawing rule
        assert "\u2713" not in source
        assert "\u2500" not in source
        assert "\u2026" not in source                 # ellipsis
        assert "\u2014" not in source                 # em dash
