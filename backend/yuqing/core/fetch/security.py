from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    pass


Resolver = Callable[[str], Iterable[ipaddress.IPv4Address | ipaddress.IPv6Address]]
PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    return list(
        {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    )


def _unsafe(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _proxy_fake_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return isinstance(address, ipaddress.IPv4Address) and address in PROXY_FAKE_IP_NETWORK


def validate_public_url(
    url: str,
    resolver: Resolver = _resolve,
    *,
    allow_proxy_fake_ip: bool = False,
) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise UnsafeUrlError("仅允许 http/https URL")
    if not parts.hostname or parts.username or parts.password:
        raise UnsafeUrlError("URL 主机名无效或包含凭据")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("URL 端口无效") from exc
    if port is not None and port not in {80, 443}:
        raise UnsafeUrlError("禁止访问非常规端口")

    try:
        literal = ipaddress.ip_address(parts.hostname)
        addresses = [literal]
        hostname_is_literal = True
    except ValueError:
        hostname_is_literal = False
        try:
            addresses = list(resolver(parts.hostname))
        except OSError as exc:
            raise UnsafeUrlError("域名解析失败") from exc
    fake_addresses = [address for address in addresses if _proxy_fake_ip(address)]
    if fake_addresses and not hostname_is_literal and not allow_proxy_fake_ip:
        raise UnsafeUrlError("域名解析到代理 Fake-IP；如确认使用 Clash/TUN，可显式启用兼容开关")
    if not addresses or any(
        _unsafe(address)
        and not (allow_proxy_fake_ip and not hostname_is_literal and _proxy_fake_ip(address))
        for address in addresses
    ):
        raise UnsafeUrlError("目标解析到内网、保留或本机地址")

    host = parts.hostname.lower()
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
