"""
factory.intake.transport
==========================
Injectable download boundary for the tool intake (Phase 2a).

The real implementation performs a single HTTPS GET against the *derived*
restricted GitHub archive URL with a byte cap and timeout. Tests inject a
fake transport — no network operation ever occurs in the test suite.

Phase 2a ships the implementation but performs **no live downloads**; it is
exercised only later (Phase 2b) under explicit approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from factory.errors import IntakeFetchError

logger = logging.getLogger(__name__)


class TransportTimeoutError(IntakeFetchError):
    """Raised when the download exceeds its timeout."""
    code = "INTAKE_FETCH_TIMEOUT"


class Transport(Protocol):
    """Minimal download boundary. Implementations must be side-effect free
    beyond returning the response bytes (no redirects to arbitrary hosts,
    no credential use, no execution)."""

    def fetch(self, url: str, *, timeout_s: float, max_bytes: int) -> bytes:
        """Return the response body, or raise IntakeFetchError family."""
        ...


@dataclass
class HttpxTransport:
    """
    Default transport: one HTTPS GET via httpx with strict limits.

    * only https:// URLs (scheme re-checked here as defense in depth)
    * no redirects (redirects could leave the restricted host)
    * streamed with a hard ``max_bytes`` cutoff
    * bounded connect/read timeout
    """

    user_agent: str = "ai-software-factory-intake/0.1"

    def fetch(self, url: str, *, timeout_s: float, max_bytes: int) -> bytes:
        if not url.lower().startswith("https://"):
            raise IntakeFetchError(f"Refusing non-HTTPS download: {url!r}")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — httpx is a dependency
            raise IntakeFetchError("httpx is not installed; cannot download.") from exc

        try:
            with httpx.Client(
                timeout=timeout_s,
                follow_redirects=False,
                headers={"User-Agent": self.user_agent},
            ) as client:
                with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise IntakeFetchError(
                            f"Download failed with HTTP {response.status_code}."
                        )
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise IntakeFetchError(
                            f"Remote archive exceeds the archive-size cap "
                            f"({max_bytes} bytes)."
                        )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise IntakeFetchError(
                                f"Remote archive exceeds the archive-size cap "
                                f"({max_bytes} bytes)."
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
        except IntakeFetchError:
            raise
        except httpx.TimeoutException as exc:
            raise TransportTimeoutError(
                f"Download timed out after {timeout_s}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise IntakeFetchError(f"Download failed: {exc}") from exc
