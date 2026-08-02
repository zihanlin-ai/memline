"""Transport for endpoints behind the corporate proxy.

The integration tests below run a stub proxy on a real socket, because the bug
they guard against is invisible at any higher level: forward-proxying a
plain-HTTP POST looks like a normal call right up until the proxy drops the
body and the origin answers 400.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import unittest

import httpx

from memline.proxy import (
    TunnellingTransport,
    bypasses_proxy,
    client_for_base_url,
    proxy_url_for,
)


class _StubProxy:
    """A one-shot proxy that records how a request was routed to it.

    On CONNECT it answers 200 and then speaks HTTP/1.1 as if it were the
    origin, so ``tunnelled`` holds exactly what came down the tunnel. Anything
    else is recorded as ``forwarded`` — the request line of a forward-proxied
    request carries the absolute URL.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self.connect_target: str | None = None
        self.tunnelled: tuple[str, str] | None = None
        self.forwarded: tuple[str, str] | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _read_request(self, stream) -> tuple[str, str]:
        request_line = stream.readline().decode().strip()
        length = 0
        while True:
            line = stream.readline().decode().strip()
            if not line:
                break
            name, _, value = line.partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        return request_line, stream.read(length).decode()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:  # pragma: no cover - closed before anyone connected
            return
        with conn:
            stream = conn.makefile("rwb")
            request_line, body = self._read_request(stream)
            if request_line.startswith("CONNECT"):
                self.connect_target = request_line.split()[1]
                stream.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                stream.flush()
                self.tunnelled = self._read_request(stream)
            else:
                self.forwarded = (request_line, body)
            payload = b'{"ok": true}'
            stream.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\nConnection: close\r\n\r\n%b"
                % (len(payload), payload)
            )
            stream.flush()

    def close(self) -> None:
        self._sock.close()


class NoProxyMatchingTests(unittest.TestCase):
    """httpx understands only some of these forms, which is why we match here."""

    def test_exact_host(self):
        self.assertTrue(bypasses_proxy("relay.internal", "other,relay.internal"))
        self.assertFalse(bypasses_proxy("relay.internal", "other,relay.external"))

    def test_domain_suffix_with_or_without_the_leading_dot(self):
        self.assertTrue(bypasses_proxy("host.example.com", ".example.com"))
        self.assertTrue(bypasses_proxy("host.example.com", "example.com"))
        self.assertFalse(bypasses_proxy("notexample.com", "example.com"))

    def test_glob_entries(self):
        self.assertTrue(bypasses_proxy("10.1.2.3", "10.*"))
        self.assertTrue(bypasses_proxy("host.huawei.com", "*.huawei.com"))
        self.assertFalse(bypasses_proxy("1.95.37.146", "10.*,100.10*,172.*,7.*"))

    def test_wildcard_bypasses_everything(self):
        self.assertTrue(bypasses_proxy("anything", "*"))

    def test_empty_entries_are_ignored(self):
        self.assertFalse(bypasses_proxy("relay.internal", " , ,"))


class _ProxyEnvSandbox:
    """Proxy variables are process-wide; hand every test a clean set."""

    def setUp(self):
        self._orig = {
            k: os.environ.get(k)
            for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY")
        }
        for key in self._orig:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._orig.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


class ProxyResolutionTests(_ProxyEnvSandbox, unittest.TestCase):
    def test_scheme_selects_the_proxy_variable(self):
        os.environ["http_proxy"] = "http://p:3128"
        os.environ["https_proxy"] = "http://s:3128"
        self.assertEqual(proxy_url_for("http://relay:3000/v1"), "http://p:3128")
        self.assertEqual(proxy_url_for("https://api.vendor.com/v1"), "http://s:3128")

    def test_uppercase_variable_is_honoured(self):
        os.environ["HTTP_PROXY"] = "http://p:3128"
        self.assertEqual(proxy_url_for("http://relay:3000/v1"), "http://p:3128")

    def test_no_proxy_glob_wins_over_the_proxy_variable(self):
        os.environ["http_proxy"] = "http://p:3128"
        os.environ["no_proxy"] = "10.*"
        self.assertIsNone(proxy_url_for("http://10.1.2.3:8000/v1"))

    def test_no_proxy_at_all_means_a_direct_route(self):
        self.assertIsNone(proxy_url_for("http://relay:3000/v1"))


class ClientSelectionTests(_ProxyEnvSandbox, unittest.TestCase):
    def test_https_keeps_the_sdk_default_client(self):
        """httpx already CONNECT-tunnels https, so nothing needs replacing."""
        os.environ["https_proxy"] = "http://p:3128"
        self.assertIsNone(client_for_base_url("https://openrouter.ai/api/v1"))

    def test_proxied_plain_http_gets_a_tunnelling_transport(self):
        os.environ["http_proxy"] = "http://p:3128"
        client = client_for_base_url("http://relay:3000/v1")
        self.assertIsInstance(client._transport, TunnellingTransport)
        client.close()

    def test_a_socks_proxy_keeps_the_default_client(self):
        """SOCKS tunnels everything already; there is no request to rewrite."""
        os.environ["http_proxy"] = "socks5://p:1080"
        self.assertIsNone(client_for_base_url("http://relay:3000/v1"))

    def test_bypassed_plain_http_gets_a_client_that_ignores_the_environment(self):
        os.environ["http_proxy"] = "http://p:3128"
        os.environ["no_proxy"] = "10.*"
        client = client_for_base_url("http://10.1.2.3:8000/v1")
        self.assertNotIsInstance(client._transport, TunnellingTransport)
        self.assertFalse(client.trust_env)
        client.close()


class TunnelTransportTests(unittest.TestCase):
    """The regression itself: a POST body must survive the proxy hop."""

    def setUp(self):
        self.proxy = _StubProxy()
        self.addCleanup(self.proxy.close)

    def test_plain_http_post_is_tunnelled_with_its_body_intact(self):
        with httpx.Client(
            transport=TunnellingTransport(self.proxy.url), timeout=10.0
        ) as client:
            response = client.post(
                "http://relay.invalid:3000/v1/chat/completions", json={"model": "m"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.proxy.connect_target, "relay.invalid:3000")
        request_line, body = self.proxy.tunnelled
        # Origin-form target, not the absolute URL a forward proxy would see.
        self.assertEqual(request_line, "POST /v1/chat/completions HTTP/1.1")
        self.assertEqual(json.loads(body), {"model": "m"})

    def test_httpx_still_forward_proxies_plain_http_on_its_own(self):
        """Why the transport above exists. If this ever fails, httpx started
        tunnelling plain HTTP itself and TunnellingTransport can be retired."""
        with httpx.Client(proxy=self.proxy.url, timeout=10.0) as client:
            client.post("http://relay.invalid:3000/v1/chat/completions", json={"model": "m"})
        self.assertIsNone(self.proxy.connect_target)
        request_line, _ = self.proxy.forwarded
        self.assertEqual(
            request_line, "POST http://relay.invalid:3000/v1/chat/completions HTTP/1.1"
        )


if __name__ == "__main__":
    unittest.main()
