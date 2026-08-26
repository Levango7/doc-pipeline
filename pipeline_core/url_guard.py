"""
URL 安全防护模块
================
对外部 URL 做 SSRF 校验：仅允许 http/https，拒绝私网/保留/链路本地 IP，
域名先解析全部 A/AAAA 记录逐个校验（防 DNS rebinding 第一层）。
"""
import ipaddress
import socket
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
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = _resolve_hostname(host)
        except (OSError, UnicodeError) as e:
            return False, f"DNS 解析失败({host!r}): {e}"
        if not resolved:
            return False, f"DNS 未返回可用地址: {host!r}"
        for ip_str in resolved:
            try:
                addr = ipaddress.ip_address(ip_str)
            except ValueError:
                return False, f"DNS 返回非法地址 {ip_str!r}"
            if _is_blocked_ip(addr):
                return False, f"{host!r} 解析到私有/保留地址 {ip_str!r}，已拒绝"
        return True, ""
    if _is_blocked_ip(literal):
        return False, f"IP {host!r} 为私有/保留地址，已拒绝"
    return True, ""
