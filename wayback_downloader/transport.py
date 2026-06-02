from __future__ import annotations

import http.client
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.parse import quote, urlsplit

from .models import HTTPResponse


class ArchiveTransport(Protocol):
    def get(self, url: str, headers: Mapping[str, str] | None = None) -> HTTPResponse:
        ...

    def close(self) -> None:
        ...


@dataclass(slots=True)
class _ConnectionEntry:
    connection: http.client.HTTPConnection
    created_at: float


class HTTPArchiveTransport:
    def __init__(self, timeout: float, *, max_age: float = 300.0) -> None:
        self.timeout = timeout
        self.max_age = max_age
        self._local = threading.local()
        self._registry_lock = threading.Lock()
        self._registry: list[http.client.HTTPConnection] = []

    def get(self, url: str, headers: Mapping[str, str] | None = None) -> HTTPResponse:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            raise ValueError(f"Invalid URL: {url}")

        connection = self._connection_for(parts.scheme, parts.hostname, parts.port)
        path = quote(parts.path or "/", safe="/:%")
        if parts.query:
            path = f"{path}?{quote(parts.query, safe='=&:%+;/,')}"

        try:
            connection.request("GET", path, headers=dict(headers or {}))
            response = connection.getresponse()
            body = response.read()
            normalized_headers = {key.lower(): value for key, value in response.getheaders()}
            return HTTPResponse(
                status=response.status,
                reason=response.reason,
                headers=normalized_headers,
                body=body,
            )
        except (OSError, ssl.SSLError, http.client.HTTPException):
            self._drop_connection(parts.scheme, parts.hostname, parts.port)
            raise

    def close(self) -> None:
        with self._registry_lock:
            while self._registry:
                connection = self._registry.pop()
                try:
                    connection.close()
                except OSError:
                    continue

    def _connection_for(self, scheme: str, host: str, port: int | None) -> http.client.HTTPConnection:
        connections = getattr(self._local, "connections", None)
        if connections is None:
            connections = {}
            self._local.connections = connections

        key = (scheme, host, port or (443 if scheme == "https" else 80))
        entry = connections.get(key)
        if entry is None or (time.monotonic() - entry.created_at) > self.max_age:
            if entry is not None:
                try:
                    entry.connection.close()
                except OSError:
                    pass
            entry = _ConnectionEntry(self._create_connection(*key), time.monotonic())
            connections[key] = entry
        return entry.connection

    def _drop_connection(self, scheme: str, host: str, port: int | None) -> None:
        connections = getattr(self._local, "connections", None)
        if not connections:
            return
        key = (scheme, host, port or (443 if scheme == "https" else 80))
        entry = connections.pop(key, None)
        if entry is None:
            return
        try:
            entry.connection.close()
        except OSError:
            pass

    def _create_connection(self, scheme: str, host: str, port: int) -> http.client.HTTPConnection:
        if scheme == "https":
            connection = http.client.HTTPSConnection(host, port, timeout=self.timeout)
        else:
            connection = http.client.HTTPConnection(host, port, timeout=self.timeout)
        with self._registry_lock:
            self._registry.append(connection)
        return connection
