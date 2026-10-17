"""
factory.tools.command
======================
Deliberate, non-shell command parsing for model-proposed commands (SEC-002).

Model-proposed command strings are never handed to an OS shell. Instead:

1. A **quote-aware scanner** rejects strings that contain UNSQUOTED shell
   operators (``;``, ``&``, ``|``, ``<``, ``>``, backtick, ``$(``, ``${``,
   newline). Operators inside quoted argument text are legal argument
   content and are preserved. Unsupported operators cause an explicit,
   fail-closed rejection — they are never reinterpreted as arguments.
2. The string is tokenized with :func:`shlex.split`. On Windows,
   non-POSIX splitting is used so backslash path separators survive
   tokenization; outer quotes are stripped from each token afterwards.
   On POSIX, POSIX splitting rules apply.
3. **Explicit shells and shell wrappers are rejected**: ``cmd``, ``bash``,
   ``sh``, ``powershell``, ``pwsh``, ``fish``, ``zsh``, and friends can
   re-introduce the very operator semantics this module removes, so they
   are refused at parse time rather than executed.
   Windows batch scripts (``.bat``/``.cmd``) are refused for the same
   reason (CreateProcess implicitly routes them through ``cmd.exe``).
4. The executable token is **resolved to an absolute path** (PATH search
   via :func:`shutil.which`, or a direct file when the token contains a
   path separator) BEFORE execution, so the exact program that will run
   is known before risk classification and approval.

What is classified and approved (the canonical argv string) is exactly
what executes — no shell interpretation happens in between.

Documented limitations
----------------------
* This is intentionally NOT a general shell: pipes, redirections,
  command substitution, ``&&``/``;`` chaining, environment assignments
  (``FOO=bar cmd``), and shell builtins (``echo``, ``dir`` as builtins)
  are unsupported by design. Use explicit programs instead.
* Quoted argument text may legitimately contain operator characters;
  those are preserved verbatim as argument content and never executed.
* ``python -c``, ``node -e`` and similar interpreter one-liners remain
  possible; their risk is governed by the RiskEngine and the permission
  broker like any other command.
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "CommandParseError",
    "ParsedCommand",
    "canonicalize_argv",
    "find_unquoted_operator",
    "parse_command",
    "reject_executable_token",
    "resolve_executable",
]


class CommandParseError(ValueError):
    """Raised when a command string is rejected by the parser."""


# ---------------------------------------------------------------------------
# Rejection tables
# ---------------------------------------------------------------------------

# Unquoted shell operators — their presence means the string needs a
# real shell, which we refuse to provide.
_OPERATOR_CHARS: tuple[str, ...] = (";", "|", "&", "<", ">", "`")

# Command/variable expansion constructs (unquoted) — refused.
_EXPANSION_MARKERS: tuple[str, ...] = ("$(", "${")

# Interpreters that ARE shells (or wrap one) — executing them re-introduces
# every operator semantic this module rejects. Matched on the basename of
# both the raw token and the resolved executable, case-insensitively.
_SHELL_EXECUTABLES: frozenset[str] = frozenset({
    "cmd", "cmd.exe",
    "powershell", "powershell.exe",
    "pwsh", "pwsh.exe",
    "bash", "bash.exe",
    "sh", "sh.exe",
    "zsh", "zsh.exe",
    "fish", "fish.exe",
    "csh", "csh.exe",
    "tcsh", "tcsh.exe",
    "ksh", "ksh.exe",
})

# Windows batch/ batch-pipeline scripts are implicitly routed through
# cmd.exe by CreateProcess — refused on Windows only.
_BATCH_EXTENSIONS: frozenset[str] = frozenset({".bat", ".cmd"})


# ---------------------------------------------------------------------------
# Quote-aware operator scan
# ---------------------------------------------------------------------------

def find_unquoted_operator(command: str) -> str | None:
    """
    Return the first UNSQUOTED shell operator/construct in ``command``.

    Single quotes make everything literal until the next single quote.
    Double quotes make everything literal except backslash-escapes of
    ``"`` and ``\\`` (the subset of POSIX rules that matters here).

    Returns the offending operator string (e.g. ``";"``, ``"$("``) or
    ``None`` when the string contains no unquoted operators.
    """
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "'":
            end = command.find("'", i + 1)
            if end == -1:
                # Unbalanced single quote — treat the rest as quoted;
                # tokenization will reject it as ambiguous.
                return None
            i = end + 1
            continue
        if ch == '"':
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n and command[i + 1] in ('"', "\\"):
                    i += 2
                    continue
                i += 1
            i += 1  # skip closing quote (or run off the end)
            continue
        if ch in ("\n", "\r"):
            return ch
        if ch in _OPERATOR_CHARS:
            return ch
        if ch == "$" and i + 1 < n and command[i + 1] in ("(", "{"):
            return command[i : i + 2]
        i += 1
    return None


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

def _strip_outer_quotes(token: str) -> str:
    """Strip one layer of matching outer quotes (Windows non-posix mode)."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _split_tokens(command: str) -> list[str]:
    """Tokenize with platform-appropriate shlex rules."""
    try:
        if os.name == "nt":
            tokens = shlex.split(command, posix=False)
            return [_strip_outer_quotes(t) for t in tokens if t != ""]
        return [t for t in shlex.split(command, posix=True) if t != ""]
    except ValueError as exc:
        raise CommandParseError(
            f"Ambiguous command (tokenizer rejected it): {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Executable resolution
# ---------------------------------------------------------------------------

def _is_shell_token(token: str) -> bool:
    """True if the raw token names a known shell interpreter."""
    return os.path.basename(token).lower() in _SHELL_EXECUTABLES


def reject_executable_token(token: str) -> str:
    """
    Structural (fail-fast) executable checks — no filesystem access.

    Raises :class:`CommandParseError` when the token names a shell
    interpreter or (on Windows) a batch script. Called during parsing,
    BEFORE risk classification and approval.
    """
    if not token:
        raise CommandParseError("Command has no executable token.")
    if _is_shell_token(token):
        raise CommandParseError(
            f"Shell interpreter {token!r} is rejected: shell semantics "
            "are not available. Propose a direct program invocation."
        )
    if os.name == "nt" and token.lower().endswith(tuple(_BATCH_EXTENSIONS)):
        raise CommandParseError(
            f"Batch script {token!r} is rejected: batch files execute "
            "through a shell."
        )
    return token


def resolve_executable(token: str, *, cwd: Path | None = None) -> str:
    """
    Resolve the executable token to an absolute path.

    * Token containing a path separator: resolved against ``cwd`` (for
      relative paths) and must point at an existing file.
    * Otherwise: PATH search via :func:`shutil.which`.

    Raises :class:`CommandParseError` when the token is a shell
    interpreter, a Windows batch script, or cannot be resolved.
    """
    if not token:
        raise CommandParseError("Command has no executable token.")

    reject_executable_token(token)

    has_sep = (os.sep in token) or (os.altsep is not None and os.altsep in token)
    if has_sep:
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = (cwd or Path.cwd()) / candidate
        try:
            candidate = candidate.resolve()
        except OSError as exc:
            raise CommandParseError(
                f"Executable path {token!r} cannot be resolved: {exc}"
            ) from exc
        if not candidate.is_file():
            raise CommandParseError(f"Executable not found: {token!r}")
        if candidate.suffix.lower() in _BATCH_EXTENSIONS and os.name == "nt":
            raise CommandParseError(
                f"Batch script {token!r} is rejected: batch files execute "
                "through a shell."
            )
        return str(candidate)

    resolved = shutil.which(token)
    if not resolved:
        raise CommandParseError(f"Executable not found on PATH: {token!r}")
    lower = resolved.lower()
    if os.name == "nt" and lower.endswith(tuple(_BATCH_EXTENSIONS)):
        raise CommandParseError(
            f"Batch script {token!r} is rejected: batch files execute "
            "through a shell."
        )
    return os.path.abspath(resolved)


# ---------------------------------------------------------------------------
# Canonical form + full parse
# ---------------------------------------------------------------------------

def canonicalize_argv(argv: list[str]) -> str:
    """
    Return a deterministic, shell-quoted rendering of ``argv``.

    This canonical string is what the RiskEngine classifies and what the
    permission broker records — it describes the exact operation that
    will execute (program + every argument), with no ambiguity.
    """
    if os.name == "nt":
        # shlex.quote is POSIX-only in spirit; on Windows use double
        # quotes around tokens containing whitespace or quotes.
        parts = []
        for a in argv:
            if any(c in a for c in (' ', '"', "\t")):
                parts.append('"' + a.replace('"', '\\"') + '"')
            else:
                parts.append(a)
        return " ".join(parts)
    return " ".join(shlex.quote(a) for a in argv)


@dataclass
class ParsedCommand:
    """A validated, executable-ready command."""
    argv: list[str]
    executable: str
    canonical: str
    raw: str
    rejected_reason: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return bool(self.rejected_reason)


def parse_command(command: str) -> ParsedCommand:
    """
    Parse a model-proposed command string into a validated argv.

    Performs phases 1-2 only (operator rejection, tokenization,
    structural executable checks). Executable *resolution* is the
    caller's step after approval — see :func:`resolve_executable`.

    Fail-closed: any rejection returns a ``ParsedCommand`` with
    ``rejected=True`` and a human-readable ``rejected_reason`` — the
    caller must not execute it. :class:`CommandParseError` is never
    raised to the caller for policy rejections.
    """
    raw = command
    if not command or not command.strip():
        return ParsedCommand(
            argv=[], executable="", canonical="", raw=raw,
            rejected_reason="Empty command.",
        )

    operator = find_unquoted_operator(command)
    if operator is not None:
        return ParsedCommand(
            argv=[], executable="", canonical="", raw=raw,
            rejected_reason=(
                f"Unsupported shell operator {operator!r}: pipelines, "
                "redirections, chaining, and substitution are not "
                "available. Propose a single direct command."
            ),
        )

    try:
        tokens = _split_tokens(command)
    except CommandParseError as exc:
        return ParsedCommand(
            argv=[], executable="", canonical="", raw=raw,
            rejected_reason=str(exc),
        )

    if not tokens:
        return ParsedCommand(
            argv=[], executable="", canonical="", raw=raw,
            rejected_reason="Command produced no tokens.",
        )

    try:
        reject_executable_token(tokens[0])
    except CommandParseError as exc:
        return ParsedCommand(
            argv=[], executable="", canonical="", raw=raw,
            rejected_reason=str(exc),
        )

    argv = list(tokens)
    return ParsedCommand(
        argv=argv,
        executable="",
        canonical=canonicalize_argv(argv),
        raw=raw,
    )
