"""url_guard.validate_public_http_url 纯单元测试：私网段 / IPv6 / localhost / 公网域 / DNS 失败"""

import socket

import pytest

from pipeline_core.url_guard import validate_public_http_url


def _dns_entries(*ips):
    entries = []
    for ip in ips:
        if ":" in ip:
            entries.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 80, 0, 0)))
        else:
            entries.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80)))
    return entries


def _install_dns(monkeypatch, mapping):
    """把 url_guard 的 DNS 解析替换为确定性映射；未登记域名按解析失败处理"""
    import pipeline_core.url_guard as url_guard

    def fake_getaddrinfo(host, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(-2, f"mock dns: {host} not resolvable")
        return _dns_entries(*mapping[host])

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_getaddrinfo)


class TestSchemeAndFormat:
    @pytest.mark.parametrize("url", [
        "",
        "   ",
        None,
        123,
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "gopher://127.0.0.1:70/x",
        "http:///path-only",
        "not a url at all",
    ])
    def test_invalid_scheme_or_format_rejected(self, url):
        ok, reason = validate_public_http_url(url)
        assert not ok
        assert reason

    def test_uppercase_scheme_accepted(self, monkeypatch):
        _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
        ok, reason = validate_public_http_url("HTTPS://example.com/")
        assert ok, reason


class TestPrivateIPv4Literals:
    @pytest.mark.parametrize("host", [
        "127.0.0.1",
        "127.8.8.8",
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "0.1.2.3",
    ])
    def test_private_ranges_rejected(self, host):
        ok, reason = validate_public_http_url(f"http://{host}/x")
        assert not ok, host
        assert reason

    def test_public_literal_ip_accepted_without_dns(self, monkeypatch):
        import pipeline_core.url_guard as url_guard

        def _no_dns(*args, **kwargs):
            raise AssertionError("公网 IP 字面量不应触发 DNS 解析")

        monkeypatch.setattr(url_guard.socket, "getaddrinfo", _no_dns)
        ok, reason = validate_public_http_url("http://8.8.8.8/dns-query")
        assert ok, reason

    def test_public_boundary_172_32_accepted(self):
        ok, reason = validate_public_http_url("http://172.32.0.1/")
        assert ok, reason

    @pytest.mark.parametrize("host", [
        "::1",
        "::",
        "fc00::1",
        "fd12:3456:789a::1",
        "fe80::1",
    ])
    def test_ipv6_private_rejected(self, host):
        ok, reason = validate_public_http_url(f"http://[{host}]/x")
        assert not ok, host
        assert reason

    def test_ipv6_public_literal_accepted(self):
        ok, reason = validate_public_http_url("http://[2606:4700::1111]/")
        assert ok, reason


class TestLocalHostnames:
    @pytest.mark.parametrize("url", [
        "http://localhost/x",
        "http://LOCALHOST:8910/admin",
        "http://localhost./x",
        "http://foo.localhost/x",
        "http://api.local/v1",
        "http://metadata.local/latest/meta-data/",
    ])
    def test_local_hostnames_rejected(self, url):
        ok, reason = validate_public_http_url(url)
        assert not ok, url
        assert reason


class TestHostnameResolution:
    def test_public_domain_resolving_to_public_ip_accepted(self, monkeypatch):
        _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
        ok, reason = validate_public_http_url("http://example.com/page")
        assert ok, reason

    def test_all_a_records_validated(self, monkeypatch):
        _install_dns(monkeypatch, {"multi.example.com": ["93.184.216.34", "2606:4700::1111"]})
        ok, reason = validate_public_http_url("https://multi.example.com/")
        assert ok, reason

    def test_any_private_record_rejects_domain(self, monkeypatch):
        _install_dns(monkeypatch, {"rebind.example.com": ["93.184.216.34", "10.0.0.5"]})
        ok, reason = validate_public_http_url("http://rebind.example.com/")
        assert not ok
        assert "10.0.0.5" in reason

    def test_dns_failure_rejected(self, monkeypatch):
        _install_dns(monkeypatch, {})
        ok, reason = validate_public_http_url("http://nonexistent.example.com/")
        assert not ok
        assert "DNS" in reason or "解析" in reason

    def test_decimal_obfuscated_loopback_caught_via_resolution(self, monkeypatch):
        _install_dns(monkeypatch, {"2130706433": ["127.0.0.1"]})
        ok, reason = validate_public_http_url("http://2130706433/admin")
        assert not ok
        assert reason

    def test_userinfo_and_port_ignored(self, monkeypatch):
        _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
        ok, reason = validate_public_http_url("http://u:p@example.com:8080/x")
        assert ok, reason
