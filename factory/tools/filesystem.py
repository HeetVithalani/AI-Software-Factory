"""
factory.tools.filesystem
=========================
Workspace-sandboxed filesystem I/O tool.

Security model
--------------
* All paths are resolved and checked against workspace_root before I/O.
* Paths that escape the sandbox raise ToolError (programming error).
* Sensitive paths (detected by SensitiveDataDetector) are blocked for
  reads unless the caller has explicit broker approval.
* Writes within the sandbox are LOW risk and auto-approved.
* Writes outside the sandbox are blocked outright (ToolError).
* Deletes are HIGH risk and require explicit approval.
* Directory listings and existence checks are SAFE.

Usage::

    tool = FilesystemTool(workspace_root=Path("/project"))
    result = tool.read(Path("src/main.py"))
    result = tool.write(Path("src/feature.py"), content)
    result = tool.list_dir(Path("src/"))
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from factory.errors import ToolError
from factory.models import RiskLevel
from factory.permissions.risk import ActionContext, RiskEngine
from factory.permissions.sensitive_data import SensitiveDataDetector
from factory.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_risk_engine = RiskEngine()
_sensitive = SensitiveDataDetector()

# Max file size to read fully (4 MB)
_MAX_READ_BYTES = 4 * 1024 * 1024


class FilesystemTool(BaseTool):
    """
    Workspace-sandboxed filesystem operations.

    All paths are validated against workspace_root before any I/O occurs.
    """

    tool_name = "filesystem"

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(
        self,
        path: Path,
        *,
        task_id: str | None = None,
        encoding: str = "utf-8",
    ) -> ToolResult:
        """
        Read a file within the sandbox.

        Sensitive files are checked via SensitiveDataDetector; if sensitive,
        a broker permission check is performed before the read.
        """
        try:
            safe_path = self._sandbox_path(path)
        except ToolError as exc:
            return self._fail(str(exc), command=f"read:{path}")

        if not safe_path.exists():
            return self._fail(
                f"File not found: {safe_path}", command=f"read:{path}"
            )
        if not safe_path.is_file():
            return self._fail(
                f"Not a file: {safe_path}", command=f"read:{path}"
            )

        # Sensitive-data gate
        report = _sensitive.check_path(safe_path)
        if report.is_sensitive:
            ctx = ActionContext(
                category="file.read",
                command=f"read {safe_path}",
                target_path=safe_path,
                target_is_sensitive=True,
            )
            decision = self._check_permission(
                ctx,
                action=f"Read sensitive file: {safe_path.name}",
                target=str(safe_path),
                reason="Agent needs to read a sensitive file",
                expected_effect="File content is read into agent memory",
                consequence_if_denied="Agent cannot access sensitive material",
                action_category="file.read",
                task_id=task_id,
            )
            if decision is not None and not decision.approved:
                message = (
                    f"Read blocked: sensitive file requires approval "
                    f"({safe_path.name})"
                )
                if decision.requires_approval:
                    # PENDING human decision — paused, not failed (COR-003)
                    req_id = (
                        decision.approval_request.request_id
                        if decision.approval_request else ""
                    )
                    return self._fail_approval(
                        message, command=f"read:{path}", request_id=req_id
                    )
                return self._fail(message, command=f"read:{path}")

        # Size guard
        size = safe_path.stat().st_size
        if size > _MAX_READ_BYTES:
            logger.warning(
                "FilesystemTool: file %s is large (%d bytes) — reading first %d bytes.",
                safe_path, size, _MAX_READ_BYTES,
            )

        try:
            content = safe_path.read_bytes()[:_MAX_READ_BYTES].decode(
                encoding, errors="replace"
            )
        except OSError as exc:
            return self._fail(str(exc), command=f"read:{path}")

        file_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        self._audit_emit("tool.filesystem.read", {
            "path": str(safe_path),
            "size_bytes": size,
            "hash": file_hash[:16],
        })

        return ToolResult(
            success=True,
            output=content,
            tool_name=self.tool_name,
            command=f"read:{safe_path}",
            evidence_hash=file_hash,
            metadata={
                "path": str(safe_path),
                "size_bytes": size,
                "encoding": encoding,
                "is_sensitive": report.is_sensitive,
            },
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(
        self,
        path: Path,
        content: str,
        *,
        task_id: str | None = None,
        encoding: str = "utf-8",
        mkdir: bool = True,
    ) -> ToolResult:
        """
        Write content to a file within the sandbox.

        Creates parent directories if mkdir=True.
        """
        try:
            safe_path = self._sandbox_path(path)
        except ToolError as exc:
            return self._fail(str(exc), command=f"write:{path}")

        ctx = ActionContext(
            category="file.write",
            command=f"write {safe_path}",
            target_path=safe_path,
            target_is_sensitive=_sensitive.is_sensitive_path(safe_path),
        )
        risk_level = _risk_engine.classify(ctx)

        # Sensitive file write → require approval
        if ctx.target_is_sensitive or risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            decision = self._check_permission(
                ctx,
                action=f"Write file: {safe_path.name}",
                target=str(safe_path),
                reason="Agent needs to write a file",
                expected_effect=f"File {safe_path.name} is created/updated",
                consequence_if_denied="File will not be written",
                action_category="file.write",
                task_id=task_id,
            )
            if decision is not None and not decision.approved:
                message = (
                    f"Write blocked: requires approval for {safe_path.name}"
                )
                if decision.requires_approval:
                    # PENDING human decision — paused, not failed (COR-003)
                    req_id = (
                        decision.approval_request.request_id
                        if decision.approval_request else ""
                    )
                    return self._fail_approval(
                        message, command=f"write:{path}", request_id=req_id
                    )
                return self._fail(message, command=f"write:{path}")

        try:
            if mkdir:
                safe_path.parent.mkdir(parents=True, exist_ok=True)
            safe_path.write_text(content, encoding=encoding)
        except OSError as exc:
            return self._fail(str(exc), command=f"write:{path}")

        file_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        self._audit_emit("tool.filesystem.write", {
            "path": str(safe_path),
            "size_bytes": len(content.encode("utf-8", errors="replace")),
            "hash": file_hash[:16],
            "risk": risk_level.value,
        })

        logger.debug("FilesystemTool: wrote %s (%d bytes).", safe_path, len(content))
        return ToolResult(
            success=True,
            output=f"Written: {safe_path}",
            tool_name=self.tool_name,
            command=f"write:{safe_path}",
            evidence_hash=file_hash,
            metadata={
                "path": str(safe_path),
                "size_bytes": len(content.encode("utf-8", errors="replace")),
                "risk_level": risk_level.value,
            },
        )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(
        self,
        path: Path,
        *,
        task_id: str | None = None,
    ) -> ToolResult:
        """
        Delete a file within the sandbox. Always requires approval (HIGH risk).
        """
        try:
            safe_path = self._sandbox_path(path)
        except ToolError as exc:
            return self._fail(str(exc), command=f"delete:{path}")

        if not safe_path.exists():
            return self._fail(
                f"File not found: {safe_path}", command=f"delete:{path}"
            )

        ctx = ActionContext(category="file.delete", command=f"delete {safe_path}")
        decision = self._check_permission(
            ctx,
            action=f"Delete file: {safe_path.name}",
            target=str(safe_path),
            reason="Agent needs to delete a file",
            expected_effect=f"File {safe_path.name} is permanently removed",
            consequence_if_denied="File will be preserved",
            action_category="file.delete",
            task_id=task_id,
        )
        if decision is not None and not decision.approved:
            message = f"Delete blocked: requires approval for {safe_path.name}"
            if decision.requires_approval:
                # PENDING human decision — paused, not failed (COR-003)
                req_id = (
                    decision.approval_request.request_id
                    if decision.approval_request else ""
                )
                return self._fail_approval(
                    message, command=f"delete:{path}", request_id=req_id
                )
            return self._fail(message, command=f"delete:{path}")

        try:
            if safe_path.is_dir():
                import shutil
                shutil.rmtree(safe_path)
            else:
                safe_path.unlink()
        except OSError as exc:
            return self._fail(str(exc), command=f"delete:{path}")

        self._audit_emit("tool.filesystem.delete", {"path": str(safe_path)})
        return self._ok(
            f"Deleted: {safe_path}",
            command=f"delete:{safe_path}",
            metadata={"path": str(safe_path)},
        )

    # ------------------------------------------------------------------
    # List directory
    # ------------------------------------------------------------------

    def list_dir(
        self,
        path: Path | None = None,
    ) -> ToolResult:
        """
        List contents of a directory within the sandbox. Always SAFE.
        """
        target = path if path is not None else (self.workspace_root or Path("."))
        try:
            safe_path = self._sandbox_path(target)
        except ToolError as exc:
            return self._fail(str(exc), command=f"list_dir:{target}")

        if not safe_path.exists():
            return self._fail(
                f"Directory not found: {safe_path}",
                command=f"list_dir:{target}",
            )
        if not safe_path.is_dir():
            return self._fail(
                f"Not a directory: {safe_path}",
                command=f"list_dir:{target}",
            )

        try:
            entries = sorted(safe_path.iterdir(), key=lambda p: (p.is_file(), p.name))
            lines = [
                f"{'DIR ' if e.is_dir() else 'FILE'} {e.name}"
                for e in entries
            ]
            output = "\n".join(lines)
        except OSError as exc:
            return self._fail(str(exc), command=f"list_dir:{target}")

        return ToolResult(
            success=True,
            output=output,
            tool_name=self.tool_name,
            command=f"list_dir:{safe_path}",
            metadata={
                "path": str(safe_path),
                "entry_count": len(lines),
                "entries": [e.name for e in entries],
            },
        )

    # ------------------------------------------------------------------
    # Exists
    # ------------------------------------------------------------------

    def exists(self, path: Path) -> bool:
        """Return True if path exists within the sandbox."""
        try:
            return self._sandbox_path(path).exists()
        except ToolError:
            return False

    def read_json(self, path: Path, **kwargs: Any) -> tuple[ToolResult, Any]:
        """
        Read a JSON file and parse it.

        Returns (ToolResult, parsed_data). parsed_data is None on error.
        """
        import json
        result = self.read(path, **kwargs)
        if not result.success:
            return result, None
        try:
            data = json.loads(result.output)
            return result, data
        except json.JSONDecodeError as exc:
            return self._fail(
                f"JSON parse error in {path}: {exc}",
                command=f"read_json:{path}",
            ), None

    def write_json(
        self, path: Path, data: Any, *, indent: int = 2, **kwargs: Any
    ) -> ToolResult:
        """Serialise data as JSON and write to path."""
        import json
        try:
            content = json.dumps(data, indent=indent, default=str)
        except (TypeError, ValueError) as exc:
            return self._fail(
                f"JSON serialise error: {exc}", command=f"write_json:{path}"
            )
        return self.write(path, content, **kwargs)
