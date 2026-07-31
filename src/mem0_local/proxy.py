"""HTTP transport for endpoints that live behind the corporate proxy.

The corporate proxy is the only route to the internal relay, and it mangles
plain-HTTP traffic: an ``http://`` origin is *forward*-proxied (the whole
absolute URL goes in the request line, body and all), and this proxy drops the
body on the way through. The relay then sees ``Content-Length: 0`` and answers
``400 invalid JSON request body`` — every call, silently, so the judges were
served entirely by the fallback endpoint.

An ``https://`` origin never hits this: httpx asks the proxy for a CONNECT
tunnel and the proxy only shovels bytes. So the fix is to use a CONNECT tunnel
for plain-HTTP origins too — same tunnel, minus the TLS handshake on top.
:func:`client_for_base_url` builds the httpx client that does it; only
``http://`` base URLs are touched, so ``https://`` endpoints keep the SDK's
own default client.

The environment's ``no_proxy`` is honoured here rather than left to httpx,
which does not understand the glob entries this workspace uses (``10.*``,
``172.*``): a host that ``no_proxy`` exempts gets a proxy-less client instead
of a tunnel, because such a host is normally reachable directly and is often
not reachable through the proxy at all.
"""

from __future__ import annotations

import fnmatch
import os
import threading
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx


def bypasses_proxy(host: str, no_proxy: str) -> bool:
    """True when ``no_proxy`` exempts ``host`` from proxying.

    Supports the three forms that appear in real ``no_proxy`` lists: an exact
    host, a ``.suffix``/bare-domain suffix, and a glob (``10.*``,
    ``*.example.com``). httpx implements only the first two, which is why this
    check exists instead of ``trust_env`` alone.
    """
    host = host.strip().strip("[]").lower()
    if not host:
        return False
    for raw in no_proxy.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if "*" in entry or "?" in entry:
            if fnmatch.fnmatch(host, entry):
                return True
            continue
        entry = entry.lstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


def proxy_url_for(url: str) -> str | None:
    """The proxy the environment wants for ``url``, or None for a direct route."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    host = parts.hostname or ""
    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    if bypasses_proxy(host, no_proxy):
        return None
    proxy = os.environ.get(f"{scheme}_proxy") or os.environ.get(f"{scheme.upper()}_PROXY")
    return proxy or None


class _PlaintextTunnelConnection(httpcore.ConnectionInterface):
    """A CONNECT tunnel carrying plain HTTP/1.1 — no TLS on top.

    httpcore's own tunnel connection always starts TLS once the proxy answers
    ``200``, because it is only ever used for ``https://`` origins. This one
    hands the tunnelled socket straight to an HTTP/1.1 connection, which is
    what an ``http://`` origin needs.
    """

    def __init__(
        self,
        proxy_origin: httpcore.Origin,
        remote_origin: httpcore.Origin,
        proxy_headers: list[tuple[bytes, bytes]] | None = None,
        keepalive_expiry: float | None = None,
        network_backend: Any = None,
    ) -> None:
        self._connection: httpcore.ConnectionInterface = httpcore.HTTPConnection(
            origin=proxy_origin,
            keepalive_expiry=keepalive_expiry,
            network_backend=network_backend,
        )
        self._proxy_origin = proxy_origin
        self._remote_origin = remote_origin
        self._proxy_headers = list(proxy_headers or [])
        self._keepalive_expiry = keepalive_expiry
        self._connect_lock = threading.Lock()
        self._connected = False

    def handle_request(self, request: httpcore.Request) -> httpcore.Response:
        with self._connect_lock:
            if not self._connected:
                target = b"%b:%d" % (self._remote_origin.host, self._remote_origin.port)
                connect_response = self._connection.handle_request(
                    httpcore.Request(
                        method=b"CONNECT",
                        url=httpcore.URL(
                            scheme=self._proxy_origin.scheme,
                            host=self._proxy_origin.host,
                            port=self._proxy_origin.port,
                            target=target,
                        ),
                        headers=[(b"Host", target), (b"Accept", b"*/*")]
                        + self._proxy_headers,
                        extensions=request.extensions,
                    )
                )
                if not 200 <= connect_response.status <= 299:
                    reason = connect_response.extensions.get("reason_phrase", b"")
                    self._connection.close()
                    raise httpcore.ProxyError(
                        f"{connect_response.status} {reason.decode('ascii', 'ignore')}"
                    )
                # The tunnel is now a raw byte pipe to the origin; speak HTTP/1.1
                # over it directly, with an origin-form request line the relay
                # understands and a body no proxy will strip.
                self._connection = httpcore.HTTP11Connection(
                    origin=self._remote_origin,
                    stream=connect_response.extensions["network_stream"],
                    keepalive_expiry=self._keepalive_expiry,
                )
                self._connected = True
        return self._connection.handle_request(request)

    def can_handle_request(self, origin: httpcore.Origin) -> bool:
        return origin == self._remote_origin

    def close(self) -> None:
        self._connection.close()

    def info(self) -> str:
        return self._connection.info()

    def is_available(self) -> bool:
        return self._connection.is_available()

    def has_expired(self) -> bool:
        return self._connection.has_expired()

    def is_idle(self) -> bool:
        return self._connection.is_idle()

    def is_closed(self) -> bool:
        return self._connection.is_closed()

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} [{self.info()}]>"


class _TunnellingProxy(httpcore.HTTPProxy):
    """``httpcore.HTTPProxy`` that tunnels plain-HTTP origins instead of forwarding."""

    def create_connection(self, origin: httpcore.Origin) -> httpcore.ConnectionInterface:
        if origin.scheme != b"http":
            return super().create_connection(origin)
        return _PlaintextTunnelConnection(
            proxy_origin=self._proxy_url.origin,
            remote_origin=origin,
            proxy_headers=self._proxy_headers,
            keepalive_expiry=self._keepalive_expiry,
            network_backend=self._network_backend,
        )


class TunnellingTransport(httpx.HTTPTransport):
    """httpx transport that reaches plain-HTTP origins through a CONNECT tunnel.

    Built by letting httpx construct its usual proxy pool and then re-basing
    that pool onto :class:`_TunnellingProxy`: every pool setting httpx derived
    (limits, proxy auth, proxy headers, socket options) is preserved, and only
    the choice of connection class changes.
    """

    def __init__(self, proxy: httpx.Proxy | str, **kwargs: Any) -> None:
        super().__init__(proxy=proxy, **kwargs)
        pool = self._pool
        if type(pool) is not httpcore.HTTPProxy:  # noqa: E721 - exact class, not a subclass
            raise RuntimeError(
                "httpx built an unexpected proxy pool "
                f"({type(pool).__name__}); CONNECT tunnelling needs httpcore.HTTPProxy"
            )
        pool.__class__ = _TunnellingProxy


def client_for_base_url(base_url: str) -> httpx.Client | None:
    """The httpx client ``base_url`` needs, or None when the default will do.

    * ``https://`` — None. httpx already CONNECT-tunnels it.
    * ``http://`` through a proxy — a tunnelling client, so the proxy cannot
      strip the request body.
    * ``http://`` exempted by ``no_proxy`` — a client with ``trust_env=False``,
      so httpx does not forward-proxy a host the environment said to reach
      directly.
    """
    if (urlsplit(base_url).scheme or "").lower() != "http":
        return None
    proxy = proxy_url_for(base_url)
    if proxy is None:
        return httpx.Client(trust_env=False)
    if urlsplit(proxy).scheme.lower().startswith("socks"):
        # A SOCKS proxy tunnels every scheme already; it never sees a request
        # to rewrite, so the default client is right.
        return None
    return httpx.Client(transport=TunnellingTransport(proxy))
