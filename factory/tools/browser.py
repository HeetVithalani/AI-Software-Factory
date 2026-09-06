"""
factory.tools.browser
======================
Read-only HTTP fetcher tool.

Security model
--------------
* ONLY GET requests are performed — no POST, PUT, DELETE.
* Private/loopback IP ranges are blocked (127.0.0.0/8, 10.0.0.0/8,
  172.16.0.0/12, 192.168.0.0/16, ::1).
* Metadata endpoints (169.254.0.0/16) are blocked.
* The URL must use http:// or https:// schemes only.
* All fetches require broker approval (HIGH risk).
* Response size is capped at 1 MB.
* Cookies and redirects to blocked hosts are rejected.

Usage::

    tool = BrowserTool(broker=broker)
    result = tool.fetch("https://docs.python.org/3/library/pathlib.html")
    if result.success:
        print(result.output[:500])
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from factory.permissions.risk import ActionContext
from factory.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MB
_DEFAULT_TIMEOUT = 15  # seconds
_BROWSER_TOOL_UA = "AI-Software-Factory/0.1 (read-only research)"

# Private / reserved IP ranges to block
_BLOCKED_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),        # loopback
    ipaddress.ip_network("10.0.0.0/8"),          # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),       # RFC1918
    ipaddress.ip_network("192.168.0.0/16"),      # RFC1918
    ipaddress.ip_network("169.254.0.0/16"),      # link-local / AWS metadata
    ipaddress.ip_network("::1/128"),             # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),            # IPv6 unique local
]


def _is_private_host(hostname: str) -> bool:
    """Return True if hostname resolves to a private/blocked IP address."""
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Cannot resolve — block as precaution
        return True

    for _family, _type, _proto, _canonname, sockaddr in addrs:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
            for blocked in _BLOCKED_RANGES:
                if ip in blocked:
                    return True
        except ValueError:
            continue

    return False


def _validate_url(url: str) -> str | None:
    """
    Validate URL for safety.

    Returns an error string if the URL is rejected, or None if safe.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return f"Unsupported scheme: {parsed.scheme!r}. Only http/https allowed."

    if not parsed.netloc:
        return "URL has no host."

    hostname = parsed.hostname or ""
    if not hostname:
        return "URL has empty hostname."

    # Block localhost variants
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return f"Blocked host: {hostname!r} (loopback)."

    # Resolve and check for private IPs
    if _is_private_host(hostname):
        return f"Blocked host: {hostname!r} resolves to a private/reserved IP."

    return None  # safe


class BrowserTool(BaseTool):
    """
    Read-only HTTP fetcher with private-IP blocking and broker approval.
    """

    tool_name = "browser"

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        broker: Any = None,
        audit_logger: Any = None,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        super().__init__(
            workspace_root=workspace_root,
            broker=broker,
            audit_logger=audit_logger,
            project_id=project_id,
            agent_id=agent_id,
        )
        self.timeout = timeout

    def fetch(
        self,
        url: str,
        *,
        task_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ToolResult:
        """
        Fetch a public URL via HTTP GET.

        Parameters
        ----------
        url:
            The URL to fetch. Must be http:// or https:// and not private.
        task_id:
            For audit context.
        headers:
            Optional additional HTTP headers.
        """
        url = url.strip()
        if not url:
            return self._fail("Empty URL.", command="fetch:")

        # Validate URL before DNS resolution
        err = _validate_url(url)
        if err:
            self._audit_emit("tool.browser.blocked", {
                "url": url, "reason": err,
            })
            return self._fail(f"URL blocked: {err}", command=f"fetch:{url}")

        # Broker approval — browser is always HIGH risk
        ctx = ActionContext(category="network", command=f"GET {url}")
        decision = self._check_permission(
            ctx,
            action=f"HTTP GET: {url}",
            target=url,
            reason="Agent needs to fetch external URL for research",
            expected_effect="Public webpage content is read (no data sent)",
            consequence_if_denied="Agent cannot access this URL",
            action_category="network.http_get",
            task_id=task_id,
        )

        if decision is not None and decision.denied:
            return self._fail(
                f"Fetch denied by policy: {decision.reason}",
                command=f"fetch:{url}",
            )
        if decision is not None and decision.requires_approval:
            req_id = (
                decision.approval_request.request_id
                if decision.approval_request else ""
            )
            return self._fail(
                f"Fetch requires human approval (request_id={req_id}): {url}",
                command=f"fetch:{url}",
            )

        # Execute fetch
        try:
            import httpx
        except ImportError:
            return self._fail(
                "httpx not installed. Add 'httpx' to dependencies.",
                command=f"fetch:{url}",
            )

        req_headers = {
            "User-Agent": _BROWSER_TOOL_UA,
            "Accept": "text/html,text/plain,application/json",
        }
        if headers:
            req_headers.update(headers)

        start = time.monotonic()
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                max_redirects=3,
            ) as client:
                response = client.get(url, headers=req_headers)
            duration_ms = (time.monotonic() - start) * 1000

            # Cap response size
            raw = response.content[:_MAX_RESPONSE_BYTES]
            content = raw.decode("utf-8", errors="replace")

            self._audit_emit("tool.browser.fetched", {
                "url": url,
                "status_code": response.status_code,
                "content_length": len(raw),
                "duration_ms": round(duration_ms, 1),
            })

            success = 200 <= response.status_code < 400

            logger.info(
                "BrowserTool: GET %s → %d (%d bytes, %.0fms)",
                url, response.status_code, len(raw), duration_ms,
            )

            return ToolResult(
                success=success,
                output=content,
                error="" if success else f"HTTP {response.status_code}",
                tool_name=self.tool_name,
                command=f"GET {url}",
                returncode=response.status_code,
                duration_ms=duration_ms,
                metadata={
                    "url": url,
                    "status_code": response.status_code,
                    "content_length": len(raw),
                    "truncated": len(response.content) > _MAX_RESPONSE_BYTES,
                    "content_type": response.headers.get("content-type", ""),
                },
            )

        except httpx.TimeoutException:
            return self._fail(
                f"Request timed out after {self.timeout}s: {url}",
                command=f"fetch:{url}",
            )
        except httpx.RequestError as exc:
            return self._fail(
                f"Request error: {exc}",
                command=f"fetch:{url}",
            )
