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


class TestDnsCache:
    """DNS 解析结果缓存（TTL/负缓存/清空/线程安全）—— 2026-08 性能优化"""

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        import pipeline_core.url_guard as url_guard
        url_guard.clear_dns_cache()
        yield
        url_guard.clear_dns_cache()

    def test_second_call_hits_cache_no_dns(self, monkeypatch):
        import pipeline_core.url_guard as url_guard
        _install_dns(monkeypatch, {"cached.com": ["93.184.216.34"]})
        ok1, r1 = validate_public_http_url("http://cached.com/a")
        assert ok1, r1
        # 第二次调用把 DNS 换成必炸实现：命中缓存则不再解析
        def _boom(*args, **kwargs):
            raise AssertionError("应命中缓存，不再触发 DNS 解析")
        monkeypatch.setattr(url_guard.socket, "getaddrinfo", _boom)
        ok2, r2 = validate_public_http_url("http://cached.com/b")
        assert ok2, r2

    def test_ttl_expiry_re_resolves(self, monkeypatch):
        import pipeline_core.url_guard as url_guard
        _install_dns(monkeypatch, {"ttl.com": ["93.184.216.34"]})
        assert validate_public_http_url("http://ttl.com/a")[0]
        # 直接把缓存项过期
        with url_guard._dns_cache_lock:
            expire_ts, ok, ips = url_guard._dns_cache["ttl.com"]
            url_guard._dns_cache["ttl.com"] = (0.0, ok, ips)
        _install_dns(monkeypatch, {"ttl.com": ["10.0.0.9"]})  # 换成私网 → 拒绝
        ok, reason = validate_public_http_url("http://ttl.com/a")
        assert not ok and "10.0.0.9" in reason

    def test_negative_cache_suppresses_retry_within_ttl(self, monkeypatch):
        import pipeline_core.url_guard as url_guard
        calls = []

        def counting_getaddrinfo(host, *args, **kwargs):
            calls.append(host)
            raise socket.gaierror(-2, "mock dns down")
        monkeypatch.setattr(url_guard.socket, "getaddrinfo", counting_getaddrinfo)
        assert not validate_public_http_url("http://down.example.com/")[0]
        assert not validate_public_http_url("http://down.example.com/2")[0]
        assert calls == ["down.example.com"]  # 负缓存期内只解析一次

    def test_private_record_rejection_not_cached_as_ok(self, monkeypatch):
        import pipeline_core.url_guard as url_guard
        _install_dns(monkeypatch, {"rebind2.com": ["10.0.0.5"]})
        assert not validate_public_http_url("http://rebind2.com/")[0]
        # 拒绝原因属于校验策略而非解析结果：解析成功被缓存（ok=True + 私网 ip），
        # 再次校验仍逐条过 _is_blocked_ip → 依旧拒绝
        def _boom(*args, **kwargs):
            raise AssertionError("解析结果应命中缓存")
        monkeypatch.setattr(url_guard.socket, "getaddrinfo", _boom)
        ok, reason = validate_public_http_url("http://rebind2.com/")
        assert not ok and "10.0.0.5" in reason

    def test_clear_dns_cache(self, monkeypatch):
        import pipeline_core.url_guard as url_guard
        _install_dns(monkeypatch, {"fresh.com": ["93.184.216.34"]})
        assert validate_public_http_url("http://fresh.com/")[0]
        url_guard.clear_dns_cache()
        assert url_guard._dns_cache == {}

    def test_cache_thread_safety(self, monkeypatch):
        """多线程并发校验同一/不同 host，无异常且结果一致"""
        import threading
        _install_dns(monkeypatch, {f"t{i}.com": ["93.184.216.34"]
                                   for i in range(10)})
        results = []
        errors = []

        def _worker(i):
            try:
                ok, _ = validate_public_http_url(f"http://t{i % 10}.com/")
                results.append(ok)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert all(results) and len(results) == 40


class TestIpCheckCache:
    """字面 IP / DNS 结果拦截判定 lru_cache（2026-08 性能优化 #6）"""

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        import pipeline_core.url_guard as url_guard
        url_guard.clear_dns_cache()
        yield
        url_guard.clear_dns_cache()

    def test_literal_ip_check_cached(self, monkeypatch):
        """同一字面 IP 重复校验只走一次 _is_blocked_ip"""
        import ipaddress as _ip

        import pipeline_core.url_guard as url_guard
        calls = []
        real = url_guard._is_blocked_ip

        def counting(addr):
            calls.append(addr)
            return real(addr)
        monkeypatch.setattr(url_guard, "_is_blocked_ip", counting)
        assert validate_public_http_url("http://8.8.8.8/a")[0]
        assert validate_public_http_url("http://8.8.8.8/b")[0]
        assert calls == [_ip.ip_address("8.8.8.8")]  # 仅判定一次

    def test_literal_private_still_rejected_after_caching(self):
        """私网字面量拒绝判定被缓存后语义不变"""
        ok1, reason1 = validate_public_http_url("http://10.0.0.5/x")
        ok2, reason2 = validate_public_http_url("http://10.0.0.5/y")
        assert not ok1 and not ok2
        assert "10.0.0.5" in reason1 and "10.0.0.5" in reason2

    def test_dns_ip_check_cached_across_hosts(self, monkeypatch):
        """不同 host 解析到同一 IP 时，拦截判定只执行一次"""
        import pipeline_core.url_guard as url_guard
        _install_dns(monkeypatch, {"h1.com": ["93.184.216.34"],
                                   "h2.com": ["93.184.216.34"]})
        calls = []
        real = url_guard._is_blocked_ip
        monkeypatch.setattr(url_guard, "_is_blocked_ip",
                            lambda a: (calls.append(a), real(a))[1])
        assert validate_public_http_url("http://h1.com/")[0]
        assert validate_public_http_url("http://h2.com/")[0]
        assert len(calls) == 1

    def test_dns_invalid_ip_still_rejected(self, monkeypatch):
        _install_dns(monkeypatch, {"badip.com": ["not-an-ip"]})
        ok, reason = validate_public_http_url("http://badip.com/")
        assert not ok and "非法地址" in reason

    def test_clear_dns_cache_clears_ip_checks(self, monkeypatch):
        import pipeline_core.url_guard as url_guard
        assert validate_public_http_url("http://8.8.8.8/")[0]
        assert url_guard._literal_ip_check.cache_info().currsize > 0
        url_guard.clear_dns_cache()
        assert url_guard._literal_ip_check.cache_info().currsize == 0
        assert url_guard._dns_ip_check.cache_info().currsize == 0
