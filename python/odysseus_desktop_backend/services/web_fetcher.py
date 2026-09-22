from __future__ import annotations

import http.client
import ipaddress
import posixpath
import socket
import ssl
import time
import urllib.parse
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from odysseus_desktop_backend.cancellation import check_cancelled


TRACKING_QUERY_KEYS = {
    "dclid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
}
ALLOWED_PAGE_CONTENT_TYPES = {
    "application/pdf",
    "application/xhtml+xml",
    "text/html",
    "text/plain",
}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
READ_CHUNK_BYTES = 64 * 1024


class FetchError(RuntimeError):
    code = "fetch_failed"


class FetchBlockedError(FetchError):
    code = "fetch_blocked"


class FetchLimitError(FetchError):
    code = "fetch_limit"


class FetchTimeoutError(FetchError):
    code = "fetch_timeout"


@dataclass(frozen=True)
class FetchResponse:
    requested_url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    bytes_downloaded: int
    redirects: int
    elapsed_ms: int

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


Resolver = Callable[[str, int], Iterable[str]]
ConnectionFactory = Callable[[str, str, int, str, float], Any]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, connect_ip: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._connect_ip = connect_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._connect_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, connect_ip: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._connect_ip = connect_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._connect_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


def _default_connection_factory(scheme: str, host: str, port: int, ip: str, timeout: float) -> Any:
    if scheme == "https":
        return _PinnedHTTPSConnection(host, port, ip, timeout)
    return _PinnedHTTPConnection(host, port, ip, timeout)


class SafeHttpFetcher:
    """Read-only, public-network-only HTTP acquisition.

    DNS is resolved and validated before every request, and the connection is
    pinned to one validated address while TLS still authenticates the original
    hostname. Redirects are followed manually and revalidated.
    """

    def __init__(
        self,
        *,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_redirects: int = 4,
        timeout_seconds: float = 15.0,
        resolver: Resolver | None = None,
        connection_factory: ConnectionFactory | None = None,
    ):
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self.max_redirects = max(0, int(max_redirects))
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self._resolver = resolver or resolve_addresses
        self._connection_factory = connection_factory or _default_connection_factory

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        allowed_content_types: set[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        clean_method = str(method or "GET").upper()
        if clean_method not in {"GET", "HEAD"}:
            raise FetchBlockedError("research acquisition only permits GET and HEAD")
        requested_url = canonicalize_url(url)
        current_url = requested_url
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        redirects = 0
        while True:
            check_cancelled()
            response = self._request_once(
                current_url,
                method=clean_method,
                deadline=deadline,
                headers=headers or {},
                allowed_content_types=allowed_content_types or ALLOWED_PAGE_CONTENT_TYPES,
            )
            if response.status not in REDIRECT_STATUSES:
                return FetchResponse(
                    requested_url=requested_url,
                    final_url=current_url,
                    status=response.status,
                    headers=response.headers,
                    body=response.body,
                    bytes_downloaded=response.bytes_downloaded,
                    redirects=redirects,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            location = response.headers.get("location", "").strip()
            if not location:
                raise FetchError("redirect response omitted Location")
            if redirects >= self.max_redirects:
                raise FetchLimitError("redirect limit exceeded")
            redirects += 1
            current_url = canonicalize_url(urllib.parse.urljoin(current_url, location))

    def _request_once(
        self,
        url: str,
        *,
        method: str,
        deadline: float,
        headers: dict[str, str],
        allowed_content_types: set[str],
    ) -> FetchResponse:
        parsed, host, port = validate_public_http_url(url, self._resolver)
        addresses = list(self._resolver(host, port))
        if not addresses:
            raise FetchBlockedError("hostname did not resolve")
        public_addresses = validate_public_addresses(addresses)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchTimeoutError("fetch timed out")
        connection = self._connection_factory(parsed.scheme, host, port, public_addresses[0], remaining)
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        host_header = host if port in {80, 443} else f"{host}:{port}"
        request_headers = {
            "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
            "Host": host_header,
            "User-Agent": "PotatoCS-Search/0",
        }
        request_headers.update({str(key): str(value) for key, value in headers.items()})
        try:
            connection.request(method, target, headers=request_headers)
            response = connection.getresponse()
            response_headers = {str(key).lower(): str(value) for key, value in response.getheaders()}
            if response.status in REDIRECT_STATUSES:
                return FetchResponse(url, url, int(response.status), response_headers, b"", 0, 0, 0)
            if response.status < 200 or response.status >= 300:
                raise FetchError(f"HTTP status {response.status}")
            content_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in allowed_content_types:
                raise FetchBlockedError(f"unsupported content type: {content_type or 'missing'}")
            content_length = parse_content_length(response_headers.get("content-length"))
            if content_length is not None and content_length > self.max_response_bytes:
                raise FetchLimitError("response size limit exceeded")
            body, downloaded = self._read_body(response, response_headers, deadline)
            return FetchResponse(url, url, int(response.status), response_headers, body, downloaded, 0, 0)
        except (TimeoutError, socket.timeout) as exc:
            raise FetchTimeoutError("fetch timed out") from exc
        except (http.client.HTTPException, zlib.error) as exc:
            raise FetchError("remote response was malformed") from exc
        except ssl.SSLError as exc:
            raise FetchError("TLS validation failed") from exc
        except OSError as exc:
            raise FetchError("network request failed") from exc
        finally:
            connection.close()

    def _read_body(self, response: Any, headers: dict[str, str], deadline: float) -> tuple[bytes, int]:
        encoding = headers.get("content-encoding", "").strip().lower()
        if encoding not in {"", "identity", "gzip", "deflate"}:
            raise FetchBlockedError(f"unsupported content encoding: {encoding}")
        decompressor: zlib.decompressobj | None = None
        if encoding == "gzip":
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decompressor = zlib.decompressobj()
        chunks: list[bytes] = []
        decoded_size = 0
        downloaded = 0
        while True:
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchTimeoutError("fetch timed out")
            sock = getattr(response, "fp", None)
            raw_socket = getattr(getattr(sock, "raw", None), "_sock", None)
            if raw_socket is not None:
                raw_socket.settimeout(remaining)
            raw = response.read(READ_CHUNK_BYTES)
            if not raw:
                break
            downloaded += len(raw)
            if downloaded > self.max_response_bytes:
                raise FetchLimitError("compressed response size limit exceeded")
            if decompressor:
                pending = raw
                while pending:
                    decoded = decompressor.decompress(
                        pending,
                        self.max_response_bytes - decoded_size + 1,
                    )
                    decoded_size += len(decoded)
                    if decoded_size > self.max_response_bytes:
                        raise FetchLimitError("decompressed response size limit exceeded")
                    chunks.append(decoded)
                    next_pending = decompressor.unconsumed_tail
                    if next_pending == pending and not decoded:
                        raise FetchLimitError("compressed response could not be bounded")
                    pending = next_pending
            else:
                decoded_size += len(raw)
                if decoded_size > self.max_response_bytes:
                    raise FetchLimitError("response size limit exceeded")
                chunks.append(raw)
        if decompressor:
            tail = decompressor.flush(self.max_response_bytes - decoded_size + 1)
            decoded_size += len(tail)
            if decoded_size > self.max_response_bytes:
                raise FetchLimitError("decompressed response size limit exceeded")
            chunks.append(tail)
        return b"".join(chunks), downloaded


def canonicalize_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        raise FetchBlockedError("URL is empty")
    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise FetchBlockedError("only http and https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise FetchBlockedError("URL user information is not allowed")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchBlockedError("URL host is missing")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise FetchBlockedError("URL host is invalid") from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise FetchBlockedError("URL port is invalid") from exc
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        raise FetchBlockedError("non-default URL ports are not allowed")
    netloc = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    raw_path = parsed.path or "/"
    normalized_path = posixpath.normpath(raw_path)
    if raw_path.endswith("/") and not normalized_path.endswith("/"):
        normalized_path += "/"
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    query_items = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key.startswith("utm_") or lower_key in TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, value))
    query_items.sort(key=lambda item: (item[0], item[1]))
    query = urllib.parse.urlencode(query_items, doseq=True)
    return urllib.parse.urlunsplit((scheme, netloc, normalized_path, query, ""))


def validate_public_http_url(url: str, resolver: Resolver = None) -> tuple[urllib.parse.SplitResult, str, int]:
    parsed = urllib.parse.urlsplit(canonicalize_url(url))
    host = str(parsed.hostname or "")
    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    addresses = list((resolver or resolve_addresses)(host, port))
    if not addresses:
        raise FetchBlockedError("hostname did not resolve")
    validate_public_addresses(addresses)
    return parsed, host, port


def validate_public_addresses(addresses: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(str(value).split("%", 1)[0])
        except ValueError as exc:
            raise FetchBlockedError("hostname resolved to an invalid address") from exc
        if not address.is_global:
            raise FetchBlockedError("hostname resolved to a non-public address")
        rendered = str(address)
        if rendered not in cleaned:
            cleaned.append(rendered)
    if not cleaned:
        raise FetchBlockedError("hostname did not resolve to a public address")
    return cleaned


def resolve_addresses(host: str, port: int) -> list[str]:
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchBlockedError("hostname resolution failed") from exc
    return [str(row[4][0]) for row in rows if row and row[4]]


def parse_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
