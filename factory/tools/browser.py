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
* Redirects: automatic following is OFF. At most 3 hops are followed,
  and EVERY redirect target (resolved against the current URL) is
  re-validated — scheme, host, private-IP — before it is requested
  (SEC-003). Blocked hops fail the fetch instead of being followed.
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
from urllib.parse import urlparse, urljoin

from factory.permissions.risk import ActionContext
from factory.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MB
_DEFAULT_TIMEOUT = 15  # seconds
_BROWSER_TOOL_UA = "AI-Software-Factory/0.1 (read-only research)"
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

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
            return self._fail_approval(
                f"Fetch requires human approval (request_id={req_id}): {url}",
                command=f"fetch:{url}",
                request_id=req_id,
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
                follow_redirects=False,
            ) as client:
                response, redirect_error = self._follow_redirects(
                    client, url, req_headers
                )
            duration_ms = (time.monotonic() - start) * 1000

            if response is None:
                # Blocked or excessive redirect — never silently followed.
                self._audit_emit("tool.browser.blocked", {
                    "url": url, "reason": redirect_error,
                })
                return self._fail(redirect_error, command=f"fetch:{url}")

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

    # ------------------------------------------------------------------
    # Redirect handling (SEC-003)
    # ------------------------------------------------------------------

    def _follow_redirects(
        self, client: Any, url: str, headers: dict[str, str]
    ) -> tuple[Any, str]:
        """
        GET ``url``, re-validating EVERY redirect hop (SEC-003).

        Redirect targets are attacker-controllable: the initial URL
        validation is meaningless if a public URL can bounce the client
        to a private/metadata host. Each ``Location`` is therefore
        resolved (relative → absolute) and run through the same
        :func:`_validate_url` gate — scheme, host, and private-IP
        checks — before it is followed.

        Returns ``(response, "")`` on a normal (non-redirect) response,
        or ``(None, reason)`` when a hop was blocked or the redirect
        budget was exhausted. At most ``_MAX_REDIRECTS`` hops are
        followed.

        Known limitation (documented, not defended here): DNS
        rebinding TOCTOU — validation resolves the hostname once, then
        httpx resolves again at connect time. Closing that gap requires
        pinning the validated IP into the transport; deferred with this
        note rather than papered over.
        """
        current = url
        for _hop in range(_MAX_REDIRECTS + 1):
            response = client.get(current, headers=headers)
            if response.status_code not in _REDIRECT_STATUSES:
                return response, ""

            location = response.headers.get("location")
            if not location:
                return response, ""  # malformed redirect → treat as final

            next_url = urljoin(current, location)
            self._audit_emit("tool.browser.redirect", {
                "from": current,
                "to": next_url,
                "hop": _hop + 1,
                "status_code": response.status_code,
            })
            err = _validate_url(next_url)
            if err:
                return None, f"Redirect target blocked: {err}"
            current = next_url

        return None, (
            f"Too many redirects (max {_MAX_REDIRECTS}): {url}"
        )
