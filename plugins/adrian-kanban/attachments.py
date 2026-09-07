from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

from hermes_cli import kanban_db
from tools.url_safety import create_ssrf_safe_client

_CHUNK_SIZE = 64 * 1024
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class PreparedAttachment:
    data: bytes
    filename: str
    content_type: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.data) is not bytes:
            raise TypeError("data must be bytes")
        if type(self.filename) is not str or not self.filename.strip():
            raise ValueError("filename must be a nonblank string")
        if self.content_type is not None and (
            type(self.content_type) is not str or not self.content_type.strip()
        ):
            raise ValueError("content_type must be None or a nonblank string")


def _is_globally_routable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        return False
    return not (
        ip.is_unspecified
        or ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("url must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not contain credentials")
    if parsed.hostname is None:
        raise ValueError("url must have a hostname")
    try:
        answers = socket.getaddrinfo(
            parsed.hostname,
            None,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError("DNS resolution failed") from exc
    if not answers:
        raise ValueError("DNS resolution returned no addresses")
    if any(not _is_globally_routable(answer[4][0]) for answer in answers):
        raise ValueError("DNS resolution returned a non-global address")


def prepare_url_attachment(
    url: str,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
) -> PreparedAttachment:
    if type(url) is not str or not url.strip():
        raise ValueError("url must be a nonblank string")
    if filename is not None and (
        type(filename) is not str or not filename.strip()
    ):
        raise ValueError("filename must be None or a nonblank string")
    if content_type is not None and (
        type(content_type) is not str or not content_type.strip()
    ):
        raise ValueError("content_type must be None or a nonblank string")

    current_url = url.strip()
    redirects_followed = 0
    with create_ssrf_safe_client(
        follow_redirects=False,
        timeout=30.0,
        trust_env=False,
    ) as client:
        while True:
            _validate_url(current_url)
            with client.stream("GET", current_url) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    if redirects_followed >= _MAX_REDIRECTS:
                        raise ValueError("too many redirects")
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect without Location")
                    current_url = urljoin(current_url, location)
                    redirects_followed += 1
                    continue
                if response.status_code >= 400:
                    raise ValueError(f"HTTP error {response.status_code}")

                content_length_header = response.headers.get("content-length")
                if content_length_header is not None:
                    try:
                        content_length = int(content_length_header)
                    except ValueError as exc:
                        raise ValueError("invalid Content-Length") from exc
                    if content_length < 0:
                        raise ValueError("invalid Content-Length")
                    if content_length > kanban_db.KANBAN_ATTACHMENT_MAX_BYTES:
                        raise ValueError("attachment exceeds maximum size")

                chunks: list[bytes] = []
                total_size = 0
                for chunk in response.iter_bytes(chunk_size=_CHUNK_SIZE):
                    total_size += len(chunk)
                    if total_size > kanban_db.KANBAN_ATTACHMENT_MAX_BYTES:
                        raise ValueError("attachment exceeds maximum size")
                    chunks.append(chunk)

                resolved_filename = filename
                if resolved_filename is None:
                    basename = urlparse(current_url).path.rsplit("/", 1)[-1]
                    resolved_filename = unquote(basename) or "download"

                resolved_content_type = content_type
                if resolved_content_type is None:
                    response_type = response.headers.get("content-type")
                    if response_type:
                        resolved_content_type = (
                            response_type.split(";", 1)[0].strip() or None
                        )

                return PreparedAttachment(
                    data=b"".join(chunks),
                    filename=resolved_filename,
                    content_type=resolved_content_type,
                )
