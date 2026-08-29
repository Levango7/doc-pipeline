"""
URL 安全防护模块
================
对外部 URL 做 SSRF 校验：仅允许 http/https，拒绝私网/保留/链路本地 IP，
域名先解析全部 A/AAAA 记录逐个校验（防 DNS rebinding 第一层）。

性能：域名解析结果带 TTL 缓存（含负缓存）——fetcher 下载 20 页 × 逐跳重定向
× 重试都会重复校验同一 host，无缓存时是数百次同步 getaddrinfo（每次 10~300ms）。
缓存只缓存"解析结果"，每跳仍走完整校验流程，不改变 SSRF 防护语义。
"""
import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse

_BLOCKED_V4_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]

_BLOCKED_V6_NETWORKS = [
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# ── DNS 解析结果缓存（线程安全）──────────────────────────────
# 正结果 TTL 300s：覆盖单次任务的生命周期，避免每跳/每次重试重复解析；
# 负结果（解析失败）TTL 60s：DNS 抖动时不反复阻塞下载线程。
_DNS_CACHE_TTL_OK = 300.0
_DNS_CACHE_TTL_FAIL = 60.0
_dns_cache: dict[str, tuple[float, bool, list[str]]] = {}  # host -> (expire_ts, ok, ips)
_dns_cache_lock = threading.Lock()


def _resolve_hostname(hostname: str) -> list[str]:
    """解析域名的全部 A/AAAA 记录（去重），失败抛 OSError"""
    infos = socket.getaddrinfo(hostname, None)
    ips: list[str] = []
    for family, _, _, _, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip_str = str(sockaddr[0])
        if ip_str not in ips:
            ips.append(ip_str)
    return ips


def clear_dns_cache() -> None:
    """清空 DNS 缓存及 IP 判定缓存（供测试与运行时刷新使用）"""
    with _dns_cache_lock:
        _dns_cache.clear()
    _literal_ip_check.cache_clear()
    _dns_ip_check.cache_clear()


def _resolve_cached(hostname: str) -> tuple[bool, list[str]]:
    """带 TTL + 负缓存的域名解析。返回 (ok, ips)；ok=False 表示解析失败（近期内不再重试）。

    注意：ok=True 但某条 ip 属私网/保留段的拒绝判定不在此缓存——那属于
    校验策略而非解析结果，仍由调用方逐条执行（策略变化时缓存不污染）。
    """
    now = time.monotonic()
    with _dns_cache_lock:
        entry = _dns_cache.get(hostname)
        if entry is not None and now < entry[0]:
            return entry[1], entry[2]
    try:
        ips = _resolve_hostname(hostname)
        ok = len(ips) > 0
    except (OSError, UnicodeError):
        ips = []
        ok = False
    with _dns_cache_lock:
        _dns_cache[hostname] = (
            now + (_DNS_CACHE_TTL_OK if ok else _DNS_CACHE_TTL_FAIL), ok, ips)
    return ok, ips


def _is_blocked_ip(addr: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    networks = _BLOCKED_V4_NETWORKS if addr.version == 4 else _BLOCKED_V6_NETWORKS
    if any(addr in net for net in networks):
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


from functools import lru_cache  # noqa: E402


@lru_cache(maxsize=2048)
def _literal_ip_check(host: str) -> str | None:
    """字面 IP 快路径判定，按 host 缓存（同 IP 重复校验 O(1)）。

    返回 None=非字面 IP（走 DNS 路径）；"..."=拒绝原因；""=公网字面量放行。
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    if _is_blocked_ip(addr):
        return f"IP {host!r} 为私有/保留地址，已拒绝"
    return ""


@lru_cache(maxsize=4096)
def _dns_ip_check(ip_str: str) -> tuple[bool, bool]:
    """DNS 返回的单条 IP 判定，按 ip_str 缓存。返回 (valid, blocked)。"""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False, False
    return True, _is_blocked_ip(addr)


def validate_public_http_url(url: object) -> tuple[bool, str]:
    """校验 URL 是否为可安全请求的公网 http(s) 地址。

    Returns:
        (ok, reason): ok=True 表示通过；ok=False 时 reason 为拒绝原因。
    """
    if not isinstance(url, str) or not url.strip():
        return False, "url 为空或非字符串"
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"url 解析失败: {e}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"scheme {parsed.scheme!r} 不允许，仅支持 http/https"
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        return False, "url 缺少主机名"
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False, f"主机 {host!r} 为本机/本地域名，已拒绝"
    literal_result = _literal_ip_check(host)
    if literal_result is not None:
        if literal_result:
            return False, literal_result
        return True, ""
    # 非字面 IP：走 DNS 解析（带 TTL/负缓存）
    ok, resolved = _resolve_cached(host)
    if not ok:
        return False, f"DNS 解析失败({host!r})"
    for ip_str in resolved:
        valid, blocked = _dns_ip_check(ip_str)
        if not valid:
            return False, f"DNS 返回非法地址 {ip_str!r}"
        if blocked:
            return False, f"{host!r} 解析到私有/保留地址 {ip_str!r}，已拒绝"
    return True, ""
