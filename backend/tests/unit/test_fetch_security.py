import ipaddress

import pytest

from yuqing.core.fetch.security import UnsafeUrlError, validate_public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "ftp://example.com/file",
        "https://example.com:8443/private",
    ],
)
def test_private_or_non_http_targets_are_rejected(url):
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url, resolver=lambda _: [ipaddress.ip_address("93.184.216.34")])


def test_dns_resolution_is_checked_before_fetching():
    with pytest.raises(UnsafeUrlError):
        validate_public_url(
            "https://public-looking.example/report",
            resolver=lambda _: [ipaddress.ip_address("10.0.0.8")],
        )


def test_normal_public_https_url_is_allowed():
    result = validate_public_url(
        "https://example.com/report",
        resolver=lambda _: [ipaddress.ip_address("93.184.216.34")],
    )
    assert result == "https://example.com/report"


def test_proxy_fake_ip_requires_explicit_opt_in_for_hostname():
    def resolver(_):
        return [ipaddress.ip_address("198.18.0.39")]

    with pytest.raises(UnsafeUrlError, match="代理 Fake-IP"):
        validate_public_url("https://news.cctv.com/report", resolver=resolver)

    assert (
        validate_public_url(
            "https://news.cctv.com/report",
            resolver=resolver,
            allow_proxy_fake_ip=True,
        )
        == "https://news.cctv.com/report"
    )


def test_proxy_fake_ip_opt_in_never_allows_literal_reserved_address():
    with pytest.raises(UnsafeUrlError):
        validate_public_url("http://198.18.0.39/report", allow_proxy_fake_ip=True)
