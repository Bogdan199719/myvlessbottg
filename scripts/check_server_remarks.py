#!/usr/bin/env python3
"""Check Unicode-safe, country-aware names in generated server links."""

from urllib.parse import unquote, urlsplit

from shop_bot.modules.xui_api import (
    _build_server_remark,
    _replace_link_remark,
    connection_strings_equivalent,
)


def main() -> None:
    exact_name = "🔀 Cascade • 🇱🇻 Riga"
    assert _build_server_remark(exact_name) == exact_name
    assert _build_server_remark("Riga Premium") == "🇱🇻 Riga Premium"
    assert _build_server_remark("France 2") == "🇫🇷 France 2"
    assert _build_server_remark("Рига — резерв") == "🇱🇻 Рига — резерв"
    assert _build_server_remark("Cascade Main") == "🌐 Cascade Main"
    assert _build_server_remark("Riga\nPremium") == "🇱🇻 Riga Premium"

    link = _replace_link_remark("vless://id@example.com:443?security=tls#old", exact_name)
    assert unquote(urlsplit(link).fragment) == exact_name
    assert " " not in link
    assert connection_strings_equivalent(link, link)
    assert not connection_strings_equivalent(
        link, "vless://id@example.com:443?security=tls#Latvia"
    )

    print("Server remark checks: OK")


if __name__ == "__main__":
    main()
