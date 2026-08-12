from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    pass


Resolver = Callable[[str], Iterable[ipaddress.IPv4Address | ipaddress.IPv6Address]]


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


def validate_public_url(url: str, resolver: Resolver = _resolve) -> str:
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
    except ValueError:
        try:
            addresses = list(resolver(parts.hostname))
        except OSError as exc:
            raise UnsafeUrlError("域名解析失败") from exc
    if not addresses or any(_unsafe(address) for address in addresses):
        raise UnsafeUrlError("目标解析到内网、保留或本机地址")

    host = parts.hostname.lower()
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
