"""
factory.llm.opencode_provider
==============================
OpenCode provider for the AI Software Factory LLMRouter.

Architecture
------------
Factory LLMRouter
    → OpenCodeProvider.complete(LLMRequest)
        → opencode run --attach <server> --format json   (subprocess)
        → parse JSON event stream
        → return LLMResponse

Server lifecycle
----------------
One ``opencode serve --port <n>`` process is started at provider init and
shared for all requests.  If the server is unavailable the provider falls back
to a direct ``opencode run`` subprocess without ``--attach``.

Every subprocess call is bounded by ``request.timeout`` seconds.  On timeout
the subprocess is killed and a ``LLMResponse(success=False)`` is returned.

Design constraints
------------------
* Does NOT call Ollama directly — OpenCode manages the model.
* Does NOT bypass ``_inference_semaphore`` in LLMRouter.
* Does NOT start a new server process per LLM request.
* Does NOT log secrets or API credentials.
* Returns the same ``LLMResponse`` type as ``OllamaProvider``.
* Structured output: factory extracts and validates JSON itself.
  OpenCode is NOT trusted to enforce Pydantic schemas.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.llm.provider import LLMProvider, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if something is listening on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_opencode() -> str | None:
    """Return the resolved path to the opencode executable, or None."""
    # On Windows opencode is a .ps1 script installed via npm
    candidates = ["opencode", "opencode.ps1", "opencode.cmd"]
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None


def _build_opencode_command(executable: str) -> list[str]:
    """Build the base command list, wrapping .ps1 in powershell on Windows."""
    if sys.platform == "win32" and executable.endswith(".ps1"):
        return ["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", executable]
    return [executable]


def _build_prompt_text(request: LLMRequest) -> str:
    """
    Flatten system_prompt + messages into a single prompt string for
    ``opencode run``.  OpenCode manages its own conversation context —
    we supply the full context in one call.
    """
    parts: list[str] = []
    if request.system_prompt:
        parts.append(f"[SYSTEM]\n{request.system_prompt}")
    for msg in request.messages:
        role = msg.get("role", "user").upper()
        content = msg.get("content", "")
        parts.append(f"[{role}]\n{content}")
    # If a JSON output schema is required, append explicit instruction
    if request.output_schema:
        schema_str = json.dumps(request.output_schema, indent=2)
        parts.append(
            "[INSTRUCTION]\n"
            "Respond with a single valid JSON object that conforms exactly to "
            "the following JSON Schema. Do not include any text before or after "
            "the JSON object.\n\n"
            f"Schema:\n{schema_str}"
        )
    return "\n\n".join(parts)


def _extract_assistant_text(events: list[dict[str, Any]]) -> str:
    """
    Extract the final assistant message text from a list of parsed
    ``opencode run --format json`` event objects.

    Event types of interest (observed from opencode 1.18.31):
    - type == "assistant" or type == "message" with role == "assistant"
    - type == "text" with content
    - type == "content_block_delta" / "content_block"
    """
    # Collect text fragments in order
    fragments: list[str] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type", "")

        # Direct text events
        if etype in ("text", "output_text"):
            text = event.get("text") or event.get("content") or ""
            if text:
                fragments.append(str(text))
            continue

        # Assistant message with parts
        if etype in ("assistant", "message"):
            content = event.get("content", "")
            if isinstance(content, str) and content:
                fragments.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        t = part.get("text") or part.get("content") or ""
                        if t:
                            fragments.append(str(t))
            continue

        # Part-by-part streaming delta
        if etype == "content_block_delta":
            delta = event.get("delta", {})
            t = delta.get("text", "")
            if t:
                fragments.append(str(t))
            continue

        # Fallback: any event that has a top-level "content" string
        content = event.get("content")
        if isinstance(content, str) and content:
            fragments.append(content)

    return "".join(fragments)


# ---------------------------------------------------------------------------
# OpenCodeProvider
# ---------------------------------------------------------------------------

class OpenCodeProvider(LLMProvider):
    """
    Executes LLM requests via the ``opencode`` CLI.

    One ``opencode serve`` process is started at init and shared for the
    lifetime of the factory run.  Requests are submitted with::

        opencode run "<prompt>" --attach http://<host>:<port> --format json

    On server unavailability the provider falls back to::

        opencode run "<prompt>" --format json

    Parameters
    ----------
    host, port:
        Address where the managed opencode server listens.
    startup_timeout:
        Seconds to wait for ``opencode serve`` to become ready.
    manage_server:
        If True (default), this instance starts and owns the server process.
        Set to False if an external server is already running.
    cwd:
        Working directory for opencode processes (should be the factory root
        or project workspace).
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 7878,
        startup_timeout: int = 10,
        manage_server: bool = True,
        cwd: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.startup_timeout = startup_timeout
        self.manage_server = manage_server
        self.cwd = cwd or Path.cwd()

        self._server_proc: subprocess.Popen | None = None
        self._server_url: str = f"http://{host}:{port}"

        self._executable: str | None = _find_opencode()
        if self._executable is None:
            logger.warning(
                "OpenCodeProvider: opencode executable not found on PATH. "
                "Provider will always return failure."
            )

        if manage_server and self._executable:
            self._start_server()

        # Always register cleanup on exit
        atexit.register(self._stop_server)

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def _start_server(self) -> None:
        """Start opencode serve if not already running on the configured port."""
        if _port_open(self.host, self.port, timeout=1.0):
            logger.info(
                "OpenCodeProvider: server already running at %s. Re-using.",
                self._server_url,
            )
            return

        cmd = _build_opencode_command(self._executable) + [
            "serve", "--port", str(self.port),
            "--hostname", self.host,
        ]
        logger.info(
            "OpenCodeProvider: starting opencode server on %s (cmd=%s)",
            self._server_url, cmd,
        )
        try:
            self._server_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd),
                text=True,
            )
        except (OSError, FileNotFoundError) as exc:
            logger.error("OpenCodeProvider: failed to start server: %s", exc)
            self._server_proc = None
            return

        # Wait for the port to open
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if _port_open(self.host, self.port, timeout=0.5):
                logger.info(
                    "OpenCodeProvider: server ready at %s (pid=%d)",
                    self._server_url, self._server_proc.pid,
                )
                return
            time.sleep(0.3)

        logger.warning(
            "OpenCodeProvider: server did not become ready within %ds. "
            "Requests will use fallback subprocess mode.",
            self.startup_timeout,
        )

    def _stop_server(self) -> None:
        """Terminate the managed opencode serve process."""
        if self._server_proc is not None:
            try:
                self._server_proc.terminate()
                self._server_proc.wait(timeout=5)
                logger.debug(
                    "OpenCodeProvider: server process terminated (pid=%d).",
                    self._server_proc.pid,
                )
            except Exception as exc:
                logger.warning(
                    "OpenCodeProvider: error stopping server: %s", exc
                )
                try:
                    self._server_proc.kill()
                except Exception:
                    pass
            finally:
                self._server_proc = None

    @property
    def server_available(self) -> bool:
        """True if the opencode server is currently reachable."""
        return _port_open(self.host, self.port, timeout=1.0)

    # ------------------------------------------------------------------
    # LLMProvider implementation
    # ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        """
        Execute a completion via opencode run.

        Prefer the managed server (--attach).  If unavailable, fall back to
        a direct run (no --attach) with the same timeout.
        """
        request_id = str(uuid.uuid4())
        start_time = datetime.now(timezone.utc)

        if self._executable is None:
            return self._failure(
                request_id, start_time,
                "opencode executable not found on PATH",
            )

        prompt = _build_prompt_text(request)
        timeout = int(request.timeout or 120)

        # Determine model flag — strip factory "opencode:" prefix if present
        raw_model = request.model or ""
        if raw_model.startswith("opencode:"):
            oc_model = raw_model[len("opencode:"):]
        elif "/" in raw_model:
            oc_model = raw_model          # already in provider/model form
        else:
            oc_model = f"ollama/{raw_model}"  # fallback: assume ollama

        use_attach = self.server_available
        cmd = _build_opencode_command(self._executable) + [
            "run", prompt,
            "--format", "json",
            "-m", oc_model,
        ]
        if use_attach:
            cmd += ["--attach", self._server_url]

        logger.info(
            "OpenCodeProvider[%s]: running (attach=%s, model=%s, timeout=%ds)",
            request_id[:8], use_attach, oc_model, timeout,
        )
        logger.debug("OpenCodeProvider cmd: %s", cmd)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.cwd),
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "OpenCodeProvider[%s]: timed out after %ds",
                request_id[:8], timeout,
            )
            return self._failure(
                request_id, start_time,
                f"opencode run timed out after {timeout}s",
            )
        except (OSError, FileNotFoundError) as exc:
            logger.error(
                "OpenCodeProvider[%s]: subprocess error: %s",
                request_id[:8], exc,
            )
            return self._failure(request_id, start_time, str(exc))

        # Parse JSON event stream from stdout
        events: list[dict[str, Any]] = []
        tokens_in = 0
        tokens_out = 0

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                events.append(event)
                # Extract token usage if present
                if isinstance(event, dict):
                    usage = event.get("usage") or event.get("tokens") or {}
                    if isinstance(usage, dict):
                        tokens_in += int(usage.get("input_tokens", 0) or
                                         usage.get("prompt_tokens", 0))
                        tokens_out += int(usage.get("output_tokens", 0) or
                                          usage.get("completion_tokens", 0))
            except json.JSONDecodeError:
                # Non-JSON lines are OpenCode log output — ignore
                logger.debug(
                    "OpenCodeProvider: non-JSON stdout line: %r", line[:120]
                )

        content = _extract_assistant_text(events)

        # Log stderr for debugging (never store secrets)
        if result.returncode != 0:
            stderr_preview = (result.stderr or "")[:500]
            logger.warning(
                "OpenCodeProvider[%s]: non-zero returncode %d. stderr: %s",
                request_id[:8], result.returncode, stderr_preview,
            )

        success = result.returncode == 0 and bool(content)
        error_msg = "" if success else (
            f"opencode returned code {result.returncode}" if not content
            else ""
        )

        # structured_data: factory will call extract_json_from_text() separately
        structured_data: dict[str, Any] = {}
        if success and request.output_schema and content:
            try:
                structured_data = json.loads(content)
            except json.JSONDecodeError:
                # Not pure JSON — the agent's structured_output module will
                # do the extraction from the raw content string
                pass

        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000.0

        logger.info(
            "OpenCodeProvider[%s]: done (success=%s, tokens_in=%d, tokens_out=%d, "
            "duration=%.0fms)",
            request_id[:8], success, tokens_in, tokens_out, duration_ms,
        )

        return LLMResponse(
            request_id=request_id,
            content=content,
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            success=success,
            error=error_msg,
            structured_data=structured_data,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _failure(
        self,
        request_id: str,
        start_time: datetime,
        error: str,
    ) -> LLMResponse:
        end_time = datetime.now(timezone.utc)
        return LLMResponse(
            request_id=request_id,
            content="",
            start_time=start_time,
            end_time=end_time,
            duration_ms=(end_time - start_time).total_seconds() * 1000.0,
            success=False,
            error=error,
        )

    def __repr__(self) -> str:
        return (
            f"OpenCodeProvider(server={self._server_url}, "
            f"running={self.server_available})"
        )
