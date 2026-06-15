import uuid
import time
import json
from datetime import timedelta
from shop_bot.utils import time_utils
import logging
from urllib.parse import parse_qsl, quote, urlparse, urlsplit, urlunsplit
from typing import List, Dict

import requests
from py3xui import Api, Client, Inbound

from shop_bot.data_manager.database import (
    get_host,
    get_key_by_email,
    get_keys_for_host,
    update_key_by_email,
    update_key_connection_string,
    purge_missing_key,
)

logger = logging.getLogger(__name__)


# Error rate limiting: track last error per host to avoid log spam
_host_error_cache: dict[str, tuple[str, float]] = {}
_host_bearer_failure_cache: dict[str, float] = {}
_ERROR_LOG_INTERVAL = 300  # Log same error once per 5 minutes
_BEARER_FAILURE_CACHE_SECONDS = 300
_XUI_LOGIN_ATTEMPTS = 3
_XUI_LOGIN_RETRY_DELAYS_SECONDS = (1, 2)
_TRANSIENT_NETWORK_ERROR_MARKERS = (
    "connection aborted",
    "connection reset",
    "connect timeout",
    "max retries exceeded",
    "name or service not known",
    "nameresolutionerror",
    "network is unreachable",
    "read timed out",
    "temporary failure in name resolution",
    "timed out",
)

COUNTRY_FLAGS = {
    "🇱🇻": ["latvia", "latvija", "riga", "рига", "latvian"],
    "🇺🇸": [
        "usa",
        "united states",
        "america",
        "сша",
        "kansas",
        "new york",
        "los angeles",
        "chicago",
        "miami",
        "dallas",
    ],
    "🇨🇦": ["canada", "канада", "toronto", "montreal", "vancouver"],
    "🇲🇽": ["mexico", "мексика", "mexico city"],
    "🇩🇪": ["germany", "deutschland", "германия", "berlin", "frankfurt"],
    "🇳🇱": [
        "netherlands",
        "nederland",
        "niderland",
        "niderlands",
        "holland",
        "нидерланды",
        "amsterdam",
    ],
    "🇫🇷": ["france", "french", "франция", "paris"],
    "🇬🇧": [
        "uk",
        "united kingdom",
        "great britain",
        "britain",
        "england",
        "англия",
        "великобритания",
        "london",
        "лондон",
    ],
    "🇮🇹": ["italy", "italia"],
    "🇪🇸": ["spain", "españa"],
    "🇸🇪": ["sweden", "sverige"],
    "🇳🇴": ["norway", "norge"],
    "🇩🇰": ["denmark", "danmark"],
    "🇫🇮": ["finland", "suomi"],
    "🇨🇭": ["switzerland", "schweiz"],
    "🇦🇹": ["austria", "österreich"],
    "🇵🇱": ["poland", "polska"],
    "🇨🇿": ["czech", "česká"],
    "🇭🇺": ["hungary", "magyarország"],
    "🇷🇴": ["romania", "românia"],
    "🇧🇬": ["bulgaria", "българия"],
    "🇬🇷": ["greece", "ελλάδα"],
    "🇹🇷": ["turkey", "türkiye"],
    "🇵🇹": ["portugal"],
    "🇯🇵": ["japan", "nihon"],
    "🇸🇬": ["singapore"],
    "🇰🇷": ["south korea", "korea"],
    "🇹🇼": ["taiwan", "中華民國"],
    "🇭🇰": ["hong kong"],
    "🇮🇳": ["india", "भारत"],
    "🇦🇪": ["uae", "emirates"],
    "🇦🇺": ["australia"],
    "🇧🇷": ["brazil", "brasil"],
    "🇪🇪": ["estonia", "eesti", "tallinn"],
    "🇱🇹": ["lithuania", "lietuva", "vilnius"],
    "🇺🇦": ["ukraine", "україна", "kyiv", "kiev"],
    "🇰🇿": ["kazakhstan", "казахстан"],
    "🇲🇩": ["moldova", "молдова"],
    "🇧🇾": ["belarus", "беларусь"],
    "🇮🇱": ["israel", "израиль"],
}


def get_country_flag_by_host(host_name: str) -> str:
    """
    Determine country flag based on host name using a dictionary lookup.
    Checks if any alias in the dictionary is a substring of the host name.
    """
    host_lower = host_name.lower()
    logger.debug(f"Detecting flag for host: '{host_name}'")

    # Check for direct flag match in name first
    for flag in COUNTRY_FLAGS.keys():
        if flag in host_name:
            return flag

    # Check for aliases
    for flag, aliases in COUNTRY_FLAGS.items():
        for alias in aliases:
            if alias in host_lower:
                return flag

    logger.warning(
        "No country flag detected for host '%s'. Add a country, city, or flag to the host name.",
        host_name,
    )
    return "🌐"


def _build_server_remark(host_name: str) -> str:
    country_flag = get_country_flag_by_host(host_name)
    clean_server_name = (
        host_name.replace(" ", "").encode("ascii", "ignore").decode("ascii")
    )
    clean_server_name = "".join(c for c in clean_server_name if c.isalnum() or c == "_")
    clean_server_name = clean_server_name.lstrip("_")
    return f"{country_flag}{clean_server_name}"


def _replace_link_remark(connection_string: str, remark: str) -> str:
    if not connection_string or not remark:
        return connection_string

    parts = urlsplit(connection_string)
    if not parts.scheme:
        return connection_string

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            parts.query,
            quote(remark, safe=""),
        )
    )


def connection_strings_equivalent(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return left == right

    try:
        left_parts = urlsplit(left.strip())
        right_parts = urlsplit(right.strip())
    except Exception:
        return left == right

    if (
        left_parts.scheme,
        left_parts.netloc,
        left_parts.path,
    ) != (
        right_parts.scheme,
        right_parts.netloc,
        right_parts.path,
    ):
        return False

    volatile_params = {"sid", "spx"}

    def _stable_query(query: str) -> dict[str, str]:
        result = {}
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key in volatile_params:
                continue
            if key == "encryption" and value == "none":
                continue
            result[key] = value
        return result

    return _stable_query(left_parts.query) == _stable_query(right_parts.query)


def _log_host_error(host_url: str, error: Exception) -> None:
    """Log host connection errors with rate limiting to reduce log spam."""
    error_type = type(error).__name__
    error_key = f"{host_url}:{error_type}"
    error_msg = str(error)[:150]  # Truncate long messages
    now = time.time()

    # Check if we've logged this error recently
    last_error = _host_error_cache.get(error_key)
    if last_error:
        _, last_time = last_error
        if now - last_time < _ERROR_LOG_INTERVAL:
            return  # Skip duplicate error within interval

    _host_error_cache[error_key] = (error_msg, now)

    # Log concise message without full traceback for known error types
    if "SSL" in error_type or "SSL" in error_msg:
        logger.error(f"SSL error for '{host_url}': {error_msg}")
    elif "Connection" in error_type:
        logger.error(f"Connection failed to '{host_url}': {error_msg}")
    else:
        # Only log full traceback for unexpected errors
        logger.error(f"Error connecting to '{host_url}': {error_msg}", exc_info=True)


def _is_transient_network_error(error: Exception) -> bool:
    error_type = type(error).__name__.lower()
    error_msg = str(error).lower()

    if any(token in error_type for token in ("connection", "timeout")):
        return True

    return any(marker in error_msg for marker in _TRANSIENT_NETWORK_ERROR_MARKERS)


def _attach_bearer_auth(api: Api, api_token: str) -> None:
    """
    Teach py3xui's current request layer to use 3x-ui v3 Bearer tokens.

    py3xui 0.4.x only knows session-cookie requests. The 3x-ui panel now
    accepts Authorization: Bearer <apiToken> under /panel/api/* and bypasses
    CSRF for those callers. Keeping this adapter local lets existing cookie
    login keep working for older panels.
    """
    token = api_token.strip()
    if not token:
        return

    for api_part in (api.client, api.inbound, api.database, api.server):
        original_request = api_part._request_with_retry

        def _request_with_bearer(
            method, url, headers, _original=original_request, **kwargs
        ):
            auth_headers = dict(headers or {})
            auth_headers["Authorization"] = f"Bearer {token}"
            return _original(method, url, auth_headers, **kwargs)

        api_part.session = "__bearer_token__"
        api_part.cookie_name = None
        api_part._request_with_retry = _request_with_bearer

    api.session = "__bearer_token__"
    api.cookie_name = None


def _bearer_recently_failed(host_url: str) -> bool:
    failed_at = _host_bearer_failure_cache.get(host_url)
    return bool(failed_at and time.time() - failed_at < _BEARER_FAILURE_CACHE_SECONDS)


def _remember_bearer_failure(host_url: str) -> None:
    _host_bearer_failure_cache[host_url] = time.time()


def _set_cookie_auth(
    api: Api, cookie_name: str, cookie_value: str, csrf_token: str | None = None
) -> None:
    for api_part in (api.client, api.inbound, api.database, api.server):
        api_part.session = cookie_value
        api_part.cookie_name = cookie_name
        if csrf_token:
            original_request = api_part._request_with_retry

            def _request_with_csrf(
                method, url, headers, _original=original_request, **kwargs
            ):
                auth_headers = dict(headers or {})
                auth_headers["X-CSRF-Token"] = csrf_token
                return _original(method, url, auth_headers, **kwargs)

            api_part._request_with_retry = _request_with_csrf

    api.session = cookie_value
    api.cookie_name = cookie_name


def _login_with_csrf(api: Api, host_url: str, username: str, password: str) -> bool:
    """
    Login against 3x-ui builds that require a CSRF token on /login.

    Newer 3x-ui SPA pages expose /csrf-token before login. py3xui 0.4.x does
    not fetch or replay that token, so panels can return 403 on the legacy path.
    """
    session = requests.Session()
    csrf_url = f"{host_url.rstrip('/')}/csrf-token"
    login_url = f"{host_url.rstrip('/')}/login"
    try:
        csrf_response = session.get(
            csrf_url, headers={"Accept": "application/json"}, timeout=10
        )
        csrf_response.raise_for_status()
        csrf_payload = csrf_response.json()
        csrf_token = str(csrf_payload.get("obj") or "").strip()
        if not csrf_token:
            return False

        login_response = session.post(
            login_url,
            headers={
                "Accept": "application/json",
                "X-CSRF-Token": csrf_token,
            },
            json={"username": username, "password": password},
            timeout=10,
        )
        login_response.raise_for_status()
        payload = login_response.json()
        if not payload.get("success"):
            logger.warning(
                "CSRF-aware XUI login failed for '%s': %s",
                host_url,
                payload.get("msg"),
            )
            return False

        for cookie in session.cookies:
            if cookie.value:
                _set_cookie_auth(api, cookie.name, cookie.value, csrf_token=csrf_token)
                return True
    except Exception as e:
        logger.debug("CSRF-aware XUI login failed for '%s': %s", host_url, e)

    return False


def _json_string_to_dict(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else value
        except json.JSONDecodeError:
            return value
    return value


def _normalize_inbound_payload(data: dict) -> dict:
    normalized = dict(data)
    for field_name in ("settings", "streamSettings", "sniffing"):
        if field_name in normalized:
            normalized[field_name] = _json_string_to_dict(normalized[field_name])
    return normalized


def _get_inbound_list_compat(api: Api) -> list[Inbound]:
    endpoint = "panel/api/inbounds/list"
    url = api.inbound._url(endpoint)
    response = api.inbound._request_with_retry(
        requests.get,
        url,
        {"Accept": "application/json"},
    )
    inbounds_json = response.json().get("obj") or []
    return [
        Inbound.model_validate(_normalize_inbound_payload(data))
        for data in inbounds_json
    ]


def _get_inbound_by_id_compat(api: Api, inbound_id: int) -> Inbound | None:
    endpoint = f"panel/api/inbounds/get/{inbound_id}"
    url = api.inbound._url(endpoint)
    response = api.inbound._request_with_retry(
        requests.get,
        url,
        {"Accept": "application/json"},
    )
    inbound_json = response.json().get("obj")
    if not inbound_json:
        return None
    return Inbound.model_validate(_normalize_inbound_payload(inbound_json))


def login_to_host(
    host_url: str,
    username: str,
    password: str,
    inbound_id: int,
    api_token: str | None = None,
) -> tuple[Api | None, Inbound | None]:
    host_url = host_url.rstrip("/")
    token = (api_token or "").strip()

    def _load_target_inbound(api: Api) -> Inbound | None:
        inbounds: List[Inbound] = _get_inbound_list_compat(api)
        return next((inbound for inbound in inbounds if inbound.id == inbound_id), None)

    def _cookie_login_api() -> Api:
        api = Api(host=host_url, username=username, password=password)
        if _login_with_csrf(api, host_url, username, password):
            return api
        api.login()
        return api

    for attempt in range(1, _XUI_LOGIN_ATTEMPTS + 1):
        try:
            if token and not _bearer_recently_failed(host_url):
                api = Api(host=host_url, username=username, password=password)
                _attach_bearer_auth(api, token)
                try:
                    target_inbound = _load_target_inbound(api)
                except Exception as token_error:
                    _remember_bearer_failure(host_url)
                    logger.warning(
                        "XUI Bearer API auth failed for '%s': %s. Falling back to CSRF/cookie login.",
                        host_url,
                        str(token_error)[:150],
                    )
                    api = _cookie_login_api()
                    target_inbound = _load_target_inbound(api)
            else:
                api = _cookie_login_api()
                target_inbound = _load_target_inbound(api)

            if target_inbound is None:
                logger.error(
                    f"Inbound with ID '{inbound_id}' not found on host '{host_url}'"
                )
                return api, None
            return api, target_inbound
        except ValueError as ve:
            logger.error(f"Configuration error for host '{host_url}': {ve}")
            return None, None
        except Exception as e:
            is_retryable = _is_transient_network_error(e)
            if is_retryable and attempt < _XUI_LOGIN_ATTEMPTS:
                delay = _XUI_LOGIN_RETRY_DELAYS_SECONDS[attempt - 1]
                logger.warning(
                    "Transient XUI login error for '%s' on attempt %s/%s: %s. Retrying in %ss.",
                    host_url,
                    attempt,
                    _XUI_LOGIN_ATTEMPTS,
                    e,
                    delay,
                )
                time.sleep(delay)
                continue

            _log_host_error(host_url, e)
            return None, None

    return None, None


def _get_stream_network_security(inbound: Inbound) -> tuple[str, str]:
    network = "tcp"
    security = "none"
    ss = getattr(inbound, "stream_settings", None)
    if ss:
        network = getattr(ss, "network", "tcp") or "tcp"
        security = getattr(ss, "security", None) or "none"
        if getattr(ss, "reality_settings", None):
            security = "reality"
    return network, security


def _set_unlimited_traffic_fields(client: Client) -> bool:
    """
    Force client to unlimited traffic in all commonly used fields.
    Returns True if at least one field value changed.
    """
    changed = False

    for attr, value in (("total_gb", 0), ("reset", 0), ("total", 0)):
        try:
            current = getattr(client, attr, None)
            if current != value:
                setattr(client, attr, value)
                changed = True
        except Exception:
            # Some py3xui versions may not expose all fields.
            continue

    return changed


def _find_client_by_email(inbound: Inbound, email: str) -> Client | None:
    clients = getattr(getattr(inbound, "settings", None), "clients", None) or []
    for client in clients:
        if getattr(client, "email", None) == email:
            return client
    return None


def _get_client_identifier_for_protocol(protocol: str, client: Client) -> str | None:
    protocol = (protocol or "").lower()
    if protocol == "vless":
        return getattr(client, "password", None) or getattr(client, "id", None)
    if protocol == "trojan":
        return getattr(client, "password", None)
    if protocol == "shadowsocks":
        return getattr(client, "email", None)
    if protocol in {"hysteria", "hysteria2"}:
        return getattr(client, "auth", None) or getattr(client, "id", None)
    return getattr(client, "id", None)


def _raw_client_id_field_for_protocol(protocol: str) -> str:
    protocol = (protocol or "").lower()
    if protocol == "trojan":
        return "password"
    if protocol == "shadowsocks":
        return "email"
    if protocol in {"hysteria", "hysteria2"}:
        return "auth"
    return "id"


def _protocol_uses_raw_client_api(protocol: str | None) -> bool:
    """
    Protocols whose client credential fields are not represented by py3xui's
    Client model. Use 3x-ui's JSON API directly so auth/password fields match
    the panel's own schema.
    """
    return (protocol or "").lower() in {"hysteria", "hysteria2"}


def _build_client_for_inbound(
    inbound: Inbound,
    email: str,
    enable: bool,
    expiry_time: int,
    flow: str = "",
    telegram_id: str | None = None,
    client_identifier: str | None = None,
) -> tuple[Client, str]:
    """
    Build a 3x-ui client using the credential field required by the inbound protocol.

    3x-ui validates VLESS/VMess by id, Trojan by password, and Shadowsocks by
    email. Sending only id to a Trojan inbound makes the panel reject addClient
    with "empty client ID" even though the UUID is present in JSON.
    """
    protocol = (getattr(inbound, "protocol", "") or "").lower()
    identifier = client_identifier or str(uuid.uuid4())

    client_kwargs = {
        "email": email,
        "enable": enable,
        "flow": flow,
        "expiry_time": expiry_time,
        "sub_id": uuid.uuid4().hex[:16],
        "total_gb": 0,
        "reset": 0,
        "tg_id": telegram_id,
    }

    if protocol == "trojan":
        client_kwargs["password"] = identifier
    elif protocol == "shadowsocks":
        # Shadowsocks uses email as the panel-side client key. Keep a generated
        # password for panels/configs that require a non-empty credential.
        identifier = email
        client_kwargs["password"] = client_identifier or str(uuid.uuid4())
    elif protocol in {"hysteria", "hysteria2"}:
        # py3xui 0.4.0 does not model Hysteria's auth field, so Hysteria clients
        # are created via raw API helpers instead of this pydantic model.
        client_kwargs["id"] = identifier
    else:
        client_kwargs["id"] = identifier

    client = Client(**client_kwargs)
    _set_unlimited_traffic_fields(client)
    return client, identifier


def _ensure_client_identifier_for_protocol(
    protocol: str, client: Client, fallback_email: str
) -> str:
    protocol = (protocol or "").lower()

    if protocol == "trojan":
        identifier = getattr(client, "password", None)
        if not identifier:
            identifier = str(uuid.uuid4())
            client.password = identifier
        return str(identifier)

    if protocol == "shadowsocks":
        identifier = getattr(client, "email", None) or fallback_email
        client.email = identifier
        if not getattr(client, "password", None):
            client.password = str(uuid.uuid4())
        return str(identifier)

    identifier = getattr(client, "id", None)
    if not identifier:
        identifier = str(uuid.uuid4())
        client.id = identifier
    return str(identifier)


def _link_matches_inbound(link: str, inbound: Inbound) -> bool:
    try:
        parsed = urlsplit(link)
    except Exception:
        return False

    expected_scheme = (getattr(inbound, "protocol", "") or "").lower()
    if expected_scheme == "hysteria":
        expected_schemes = {"hysteria", "hysteria2"}
    else:
        expected_schemes = {expected_scheme}

    if expected_scheme and parsed.scheme.lower() not in expected_schemes:
        return False

    try:
        link_port = parsed.port
    except ValueError:
        link_port = None

    return link_port == getattr(inbound, "port", None)


def _filter_links_for_inbound(links: list[str], inbound: Inbound) -> list[str]:
    matched = [link for link in links if _link_matches_inbound(link, inbound)]
    if matched or len(links) <= 1:
        return matched or links
    logger.debug(
        "Panel returned %s links for inbound %s but none matched port/protocol.",
        len(links),
        getattr(inbound, "id", ""),
    )
    return []


def _get_client_links_from_panel(api: Api, inbound: Inbound, email: str) -> list[str]:
    """Ask 3x-ui to build protocol URLs using its official clients API."""
    try:
        endpoint = f"panel/api/clients/links/{quote(email, safe='')}"
        url = api.inbound._url(endpoint)
        response = api.inbound._request_with_retry(
            requests.get,
            url,
            {"Accept": "application/json"},
        )
        payload = response.json()
        links = [str(link) for link in (payload.get("obj") or []) if link]
        if links:
            return _filter_links_for_inbound(links, inbound)
    except Exception as e:
        logger.debug(
            "Could not get v3 panel-generated client links for '%s' on inbound %s: %s",
            email,
            getattr(inbound, "id", ""),
            e,
        )

    try:
        endpoint = (
            f"panel/api/inbounds/getClientLinks/{inbound.id}/"
            f"{quote(email, safe='')}"
        )
        url = api.inbound._url(endpoint)
        response = api.inbound._request_with_retry(
            requests.get,
            url,
            {"Accept": "application/json"},
        )
        payload = response.json()
        links = [str(link) for link in (payload.get("obj") or []) if link]
        return _filter_links_for_inbound(links, inbound)
    except Exception as e:
        logger.debug(
            "Could not get legacy panel-generated client links for '%s' on inbound %s: %s",
            email,
            getattr(inbound, "id", ""),
            e,
        )
        return []


def _protocol_prefers_panel_links(protocol: str | None) -> bool:
    """
    Protocols whose share-link format is panel-specific or not implemented
    locally. Ask 3x-ui's own link provider first for these protocols.
    """
    return (protocol or "").lower() in {
        "http",
        "mixed",
        "shadowsocks",
        "tunnel",
        "wireguard",
    }


def _inbound_prefers_panel_links(inbound: Inbound) -> bool:
    protocol = (getattr(inbound, "protocol", "") or "").lower()
    if _protocol_prefers_panel_links(protocol):
        return True

    network, _ = _get_stream_network_security(inbound)
    return protocol == "vless" and network in {"xhttp", "splithttp", "httpupgrade"}


def _connection_string_for_client(
    api: Api,
    inbound: Inbound,
    host_url: str,
    email: str,
    client_identifier: str | None,
    remark: str,
) -> str | None:
    protocol = getattr(inbound, "protocol", "") or ""
    protocol_lower = protocol.lower()
    connection_string = None

    panel_links = _get_client_links_from_panel(api, inbound, email)
    if panel_links:
        candidate = panel_links[0]
        if protocol_lower in {"vless", "trojan"} and client_identifier:
            try:
                candidate_identifier = urlsplit(candidate).username
            except Exception:
                candidate_identifier = None
            if candidate_identifier and candidate_identifier != client_identifier:
                logger.warning(
                    "Panel-generated link for '%s' on inbound %s has identifier '%s', "
                    "expected '%s'; using panel link because it matches the panel-side email.",
                    email,
                    getattr(inbound, "id", ""),
                    candidate_identifier,
                    client_identifier,
                )
                connection_string = candidate
            else:
                connection_string = candidate
        else:
            connection_string = candidate
    if connection_string is None and protocol_lower in {"hysteria", "hysteria2"} and client_identifier:
        connection_string = get_connection_string(
            inbound, client_identifier, host_url, remark=remark
        )
    elif connection_string is None and client_identifier:
        connection_string = get_connection_string(
            inbound, client_identifier, host_url, remark=remark
        )

    return _replace_link_remark(connection_string, remark)


def _raw_api_request(
    api: Api, method, endpoint: str, payload: dict | None = None
) -> dict:
    url = api.inbound._url(endpoint)
    kwargs = {}
    if payload is not None:
        kwargs["json"] = payload
    response = api.inbound._request_with_retry(
        method,
        url,
        {"Accept": "application/json"},
        **kwargs,
    )
    try:
        result = response.json()
    except ValueError as e:
        body_preview = (getattr(response, "text", "") or "").strip()[:200]
        raise RuntimeError(
            f"3x-ui returned non-JSON response from {endpoint}: {body_preview or 'empty response'}"
        ) from e
    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(result.get("msg") or f"3x-ui API request failed: {endpoint}")
    return result


def _is_endpoint_not_found_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "404" in message and "not found" in message


def _is_legacy_client_write_fallback_error(exc: Exception) -> bool:
    """Legacy 3x-ui client writes can fail with 404 or empty/non-JSON bodies."""
    if _is_endpoint_not_found_error(exc):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "empty response",
            "non-json response",
            "expecting value",
            "http/0.9",
            "invalid json",
        )
    )


def _is_duplicate_client_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in ("already", "exist", "duplicate"))


def _safe_int(value, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _client_payload_for_clients_api(client: Client, client_identifier: str) -> dict:
    return {
        "email": getattr(client, "email", "") or "",
        "subId": getattr(client, "sub_id", None) or uuid.uuid4().hex[:16],
        "id": client_identifier,
        "password": getattr(client, "password", None) or client_identifier,
        "auth": getattr(client, "auth", None) or client_identifier,
        "flow": getattr(client, "flow", None) or "",
        "totalGB": _safe_int(getattr(client, "total_gb", 0)),
        "expiryTime": _safe_int(getattr(client, "expiry_time", 0)),
        "limitIp": _safe_int(getattr(client, "limit_ip", 0)),
        "tgId": _safe_int(getattr(client, "tg_id", 0)),
        "comment": getattr(client, "comment", None) or "",
        "enable": bool(getattr(client, "enable", True)),
    }


def _raw_client_payload_for_clients_api(client: dict) -> dict:
    identifier = (
        client.get("auth")
        or client.get("id")
        or client.get("password")
        or client.get("email")
        or str(uuid.uuid4())
    )
    return {
        "email": client.get("email") or "",
        "subId": client.get("subId") or uuid.uuid4().hex[:16],
        "id": client.get("id") or identifier,
        "password": client.get("password") or identifier,
        "auth": client.get("auth") or identifier,
        "flow": client.get("flow") or "",
        "security": client.get("security") or "",
        "totalGB": _safe_int(client.get("totalGB")),
        "expiryTime": _safe_int(client.get("expiryTime")),
        "limitIp": _safe_int(client.get("limitIp")),
        "tgId": _safe_int(client.get("tgId")),
        "comment": client.get("comment") or "",
        "enable": bool(client.get("enable", True)),
        "reset": _safe_int(client.get("reset")),
    }


def _add_client_v3(
    api: Api,
    inbound_id: int,
    client_payload: dict,
    update_payload: dict | None = None,
) -> None:
    email = client_payload.get("email") or client_payload.get("id") or ""
    try:
        _raw_api_request(
            api,
            requests.post,
            "panel/api/clients/add",
            {"client": client_payload, "inboundIds": [inbound_id]},
        )
    except Exception as e:
        if not _is_duplicate_client_error(e):
            raise
        if not email:
            raise
        _raw_api_request(
            api,
            requests.post,
            f"panel/api/clients/update/{quote(str(email), safe='')}",
            update_payload or client_payload,
        )
        _raw_api_request(
            api,
            requests.post,
            f"panel/api/clients/{quote(str(email), safe='')}/attach",
            {"inboundIds": [inbound_id]},
        )


def _add_client_compat(
    api: Api, inbound_id: int, client: Client, client_identifier: str
) -> None:
    try:
        api.client.add(inbound_id, [client])
        return
    except Exception as e:
        if not _is_legacy_client_write_fallback_error(e):
            raise

    payload = _client_payload_for_clients_api(client, client_identifier)
    _add_client_v3(api, inbound_id, payload)


def _update_client_compat(api: Api, client_identifier: str, client: Client) -> None:
    try:
        api.client.update(client_identifier, client)
        return
    except Exception as e:
        if not _is_endpoint_not_found_error(e):
            raise

    email = getattr(client, "email", "") or client_identifier
    _raw_api_request(
        api,
        requests.post,
        f"panel/api/clients/update/{quote(email, safe='')}",
        _client_payload_for_clients_api(client, client_identifier),
    )


def _delete_client_compat(
    api: Api, inbound_id: int, client_identifier: str, email: str
) -> None:
    try:
        api.client.delete(inbound_id, client_identifier)
        return
    except Exception as e:
        if not _is_endpoint_not_found_error(e):
            raise

    _raw_api_request(
        api,
        requests.post,
        f"panel/api/clients/del/{quote(email or client_identifier, safe='')}",
    )


def _reset_client_traffic_compat(api: Api, inbound_id: int, email: str) -> None:
    try:
        api.client.reset_stats(inbound_id, email)
        return
    except Exception as e:
        if not _is_endpoint_not_found_error(e):
            raise

    _raw_api_request(
        api,
        requests.post,
        f"panel/api/clients/resetTraffic/{quote(email, safe='')}",
    )


def _get_client_traffic_compat(api: Api, email: str):
    try:
        return api.client.get_by_email(email)
    except Exception as e:
        if not _is_endpoint_not_found_error(e):
            raise

    payload = _raw_api_request(
        api,
        requests.get,
        f"panel/api/clients/traffic/{quote(email, safe='')}",
    )
    return payload.get("obj")


def _get_raw_inbound_obj(api: Api, inbound_id: int) -> dict:
    payload = _raw_api_request(
        api,
        requests.get,
        f"panel/api/inbounds/get/{inbound_id}",
    )
    return payload.get("obj") or {}


def _get_raw_clients(api: Api, inbound_id: int) -> list[dict]:
    inbound_obj = _get_raw_inbound_obj(api, inbound_id)
    settings_raw = inbound_obj.get("settings") or "{}"
    settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
    return settings.get("clients") or []


def _get_raw_client_identifier_by_email(
    api: Api, inbound_id: int, protocol: str, email: str
) -> str | None:
    id_field = _raw_client_id_field_for_protocol(protocol)
    for raw_client in _get_raw_clients(api, inbound_id):
        if raw_client.get("email") != email:
            continue
        if (protocol or "").lower() == "vless":
            identifier = raw_client.get("password") or raw_client.get("id")
        else:
            identifier = (
                raw_client.get(id_field)
                or raw_client.get("id")
                or raw_client.get("password")
                or raw_client.get("auth")
                or raw_client.get("email")
            )
        return str(identifier) if identifier else None
    return None


def _build_raw_client_for_protocol(
    protocol: str,
    email: str,
    enable: bool,
    expiry_time: int,
    flow: str = "",
    telegram_id: str | None = None,
    client_identifier: str | None = None,
) -> tuple[dict, str]:
    protocol = (protocol or "").lower()
    id_field = _raw_client_id_field_for_protocol(protocol)
    identifier = client_identifier or str(uuid.uuid4())

    client = {
        "email": email,
        "limitIp": 0,
        "totalGB": 0,
        "expiryTime": expiry_time,
        "enable": enable,
        "tgId": telegram_id or 0,
        "subId": uuid.uuid4().hex[:16],
        "comment": "",
        "reset": 0,
    }

    if id_field == "email":
        identifier = email
    elif id_field == "auth":
        client["security"] = ""
        client["auth"] = identifier
    elif id_field == "password":
        client["password"] = identifier
    else:
        client["id"] = identifier
        client["flow"] = flow

    return client, identifier


def _add_raw_client(api: Api, inbound_id: int, client: dict) -> None:
    try:
        _raw_api_request(
            api,
            requests.post,
            "panel/api/inbounds/addClient",
            {"id": inbound_id, "settings": json.dumps({"clients": [client]})},
        )
        return
    except Exception as e:
        if not _is_legacy_client_write_fallback_error(e):
            raise

    client_payload = _raw_client_payload_for_clients_api(client)
    _add_client_v3(api, inbound_id, client_payload)


def _update_raw_client(
    api: Api, inbound_id: int, client_identifier: str, client: dict
) -> None:
    try:
        _raw_api_request(
            api,
            requests.post,
            f"panel/api/inbounds/updateClient/{quote(client_identifier, safe='')}",
            {"id": inbound_id, "settings": json.dumps({"clients": [client]})},
        )
        return
    except Exception as e:
        if not _is_legacy_client_write_fallback_error(e):
            raise

    email = client.get("email") or client_identifier
    _raw_api_request(
        api,
        requests.post,
        f"panel/api/clients/update/{quote(email, safe='')}",
        _raw_client_payload_for_clients_api(client),
    )


def _delete_raw_client(api: Api, inbound_id: int, client_identifier: str) -> None:
    try:
        _raw_api_request(
            api,
            requests.post,
            (
                f"panel/api/inbounds/{inbound_id}/delClient/"
                f"{quote(client_identifier, safe='')}"
            ),
        )
        return
    except Exception as e:
        if not _is_endpoint_not_found_error(e):
            raise

    delete_key = client_identifier
    for raw_client in _get_raw_clients(api, inbound_id):
        raw_identifier = (
            raw_client.get("auth")
            or raw_client.get("id")
            or raw_client.get("password")
            or raw_client.get("email")
        )
        if str(raw_identifier) == str(client_identifier):
            delete_key = raw_client.get("email") or client_identifier
            break

    _raw_api_request(
        api,
        requests.post,
        f"panel/api/clients/del/{quote(delete_key, safe='')}",
    )


def _update_client_direct(api: Api, inbound_id: int, client: Client) -> bool:
    """Update one client through the same API path the 3x-ui UI uses."""
    client_uuid = getattr(client, "id", None)
    if not client_uuid:
        return False

    try:
        client.inbound_id = inbound_id
        _update_client_compat(api, str(client_uuid), client)
        return True
    except Exception as e:
        logger.warning(
            "Could not update client '%s' via client.update on inbound %s: %s",
            getattr(client, "email", ""),
            inbound_id,
            e,
        )
        return False


def _set_client_enabled_state(
    api: Api, inbound_id: int, email: str, enabled: bool
) -> bool:
    """Best-effort single-client enable toggle, preferring client.update."""
    try:
        inbound_fresh = _get_inbound_by_id_compat(api, inbound_id)
        if not inbound_fresh:
            return False
        if inbound_fresh.settings.clients is None:
            inbound_fresh.settings.clients = []

        client = _find_client_by_email(inbound_fresh, email)
        if not client:
            return False

        if bool(getattr(client, "enable", True)) == enabled:
            return True

        client.enable = enabled
        if _update_client_direct(api, inbound_id, client):
            return True

        api.inbound.update(inbound_id, inbound_fresh)
        return True
    except Exception as e:
        logger.warning(
            "Could not set enable=%s for client '%s' on inbound %s: %s",
            enabled,
            email,
            inbound_id,
            e,
        )
    return False


def _refresh_reactivated_client_visual_state(
    api: Api, inbound_id: int, email: str
) -> None:
    """
    Some 3x-ui builds keep a stale red "depleted/exhausted" badge in clientTraffics
    after an expired client is renewed. Mimic the manual client toggle from the UI.
    """
    try:
        _normalize_client_traffic_state(api, inbound_id, email)
        _set_client_enabled_state(api, inbound_id, email, False)
        time.sleep(0.15)
        _set_client_enabled_state(api, inbound_id, email, True)
        time.sleep(0.15)
        _normalize_client_traffic_state(api, inbound_id, email)
        logger.debug(
            "Refreshed visual exhausted-state for reactivated client '%s' on inbound %s",
            email,
            inbound_id,
        )
    except Exception as e:
        logger.warning(
            "Could not refresh visual exhausted-state for '%s' on inbound %s: %s",
            email,
            inbound_id,
            e,
        )


def _normalize_client_traffic_state(api: Api, inbound_id: int, email: str) -> bool:
    """Clear stale 3x-ui clientTraffics state for one client."""
    try:
        _reset_client_traffic_compat(api, inbound_id, email)
        logger.debug(
            "Traffic state reset for client '%s' on inbound %s", email, inbound_id
        )
        return True
    except Exception as rst_err:
        logger.warning(
            "Could not reset traffic stats for client '%s' on inbound %s: %s",
            email,
            inbound_id,
            rst_err,
        )
        return False


def _client_traffic_is_disabled(api: Api, email: str) -> bool:
    """Return True when clientTraffics says the client is disabled/exhausted."""
    try:
        traffic_client = _get_client_traffic_compat(api, email)
        if not traffic_client:
            return False
        if isinstance(traffic_client, dict):
            return not bool(traffic_client.get("enable", True))
        return not bool(getattr(traffic_client, "enable", True))
    except Exception as e:
        logger.debug("Could not inspect clientTraffics for '%s': %s", email, e)
        return False


def validate_host_write_access(
    host_url: str,
    username: str,
    password: str,
    inbound_id: int,
    api_token: str | None = None,
) -> tuple[bool, str]:
    """
    Verify that a 3x-ui host can both read the inbound and write clients.

    Login-only checks are not enough for recent 3x-ui builds: a panel can allow
    /inbounds/list but reject /addClient when CSRF/API permissions are wrong.
    """
    api, inbound = login_to_host(
        host_url=host_url,
        username=username,
        password=password,
        inbound_id=inbound_id,
        api_token=api_token,
    )
    if not api or not inbound:
        return False, "не удалось войти в 3x-ui или найти указанный inbound"

    probe_email = f"shopbot-preflight-{uuid.uuid4().hex[:12]}"
    protocol = (getattr(inbound, "protocol", "") or "").lower()
    probe_expiry = time_utils.get_timestamp_ms(time_utils.get_msk_now())
    if _protocol_uses_raw_client_api(protocol):
        probe_client, probe_identifier = _build_raw_client_for_protocol(
            protocol=protocol,
            email=probe_email,
            enable=False,
            expiry_time=probe_expiry,
        )
    else:
        probe_client, probe_identifier = _build_client_for_inbound(
            inbound=inbound,
            email=probe_email,
            enable=False,
            expiry_time=probe_expiry,
        )

    try:
        if _protocol_uses_raw_client_api(protocol):
            _add_raw_client(api, inbound.id, probe_client)
        else:
            _add_client_compat(api, inbound.id, probe_client, probe_identifier)
    except Exception as e:
        return False, f"панель не разрешила создать тестового клиента: {e}"

    try:
        if _protocol_uses_raw_client_api(protocol):
            _delete_raw_client(api, inbound.id, probe_identifier)
        else:
            _delete_client_compat(api, inbound.id, probe_identifier, probe_email)
    except Exception as e:
        logger.warning(
            "Host write preflight created test client '%s' but could not delete it from inbound %s: %s",
            probe_email,
            inbound.id,
            e,
        )
        return (
            False,
            "тестовый клиент был создан, но не удалился; проверьте права API и удалите "
            f"'{probe_email}' вручную",
        )

    return True, "ok"


def get_connection_string(
    inbound: Inbound, user_uuid: str, host_url: str, remark: str
) -> str | None:
    if not inbound:
        logger.error("Inbound is None")
        return None

    parsed_url = urlparse(host_url)
    port = inbound.port
    protocol = getattr(inbound, "protocol", "unknown")

    # Determine network type (transport)
    network, _ = _get_stream_network_security(inbound)

    if not port:
        logger.error("Inbound port is missing")
        return None

    # Keep original remark (including Unicode flag)
    safe_remark = remark

    logger.debug(
        f"Generating connection string - protocol: {protocol}, network: {network}, port: {port}, hostname: {parsed_url.hostname}, remark: {safe_remark}"
    )

    # Определяем тип протокола
    protocol_lower = protocol.lower()

    if protocol_lower == "vless":
        return _get_vless_connection_string(
            inbound, user_uuid, parsed_url.hostname, port, safe_remark, network
        )
    elif protocol_lower == "vmess":
        return _get_vmess_connection_string(
            inbound, user_uuid, parsed_url.hostname, port, safe_remark
        )
    elif protocol_lower == "trojan":
        return _get_trojan_connection_string(
            inbound, user_uuid, parsed_url.hostname, port, safe_remark
        )
    elif protocol_lower in {"hysteria", "hysteria2"}:
        return _get_hysteria_connection_string(
            inbound, user_uuid, parsed_url.hostname, port, safe_remark
        )
    elif _protocol_prefers_panel_links(protocol_lower):
        logger.debug(
            "Protocol '%s' uses panel-generated links; manual link builder skipped.",
            protocol,
        )
        return None
    else:
        logger.error(f"Unsupported protocol: {protocol}")
        return None


def _get_hysteria_connection_string(
    inbound: Inbound, auth: str, hostname: str, port: int, remark: str
) -> str | None:
    stream_settings = getattr(inbound, "stream_settings", None)
    tls_settings = {}
    if stream_settings:
        tls_settings = getattr(stream_settings, "tls_settings", None) or {}

    sni = tls_settings.get("serverName") or hostname
    settings = tls_settings.get("settings") or {}
    fingerprint = settings.get("fingerprint") or "chrome"
    alpn_values = tls_settings.get("alpn") or ["h3"]
    alpn = alpn_values[0] if isinstance(alpn_values, list) and alpn_values else "h3"

    if not auth or not hostname or not port:
        logger.error("Cannot build Hysteria link: auth, hostname, or port is missing.")
        return None

    query = (
        f"alpn={quote(str(alpn), safe='')}"
        f"&fp={quote(str(fingerprint), safe='')}"
        "&security=tls"
        f"&sni={quote(str(sni), safe='')}"
    )
    return f"hysteria2://{auth}@{hostname}:{port}?{query}#{quote(remark, safe='')}"


def _get_vless_connection_string(
    inbound: Inbound,
    user_uuid: str,
    hostname: str,
    port: int,
    remark: str,
    network: str,
) -> str | None:
    """Generate VLESS connection string with automatic parameter detection"""

    stream_settings = inbound.stream_settings
    logger.debug(
        f"Generating VLESS connection string for inbound protocol: {getattr(inbound, 'protocol', 'unknown')}, network: {network}, port: {port}"
    )

    # Common parameters
    base_link = f"vless://{user_uuid}@{hostname}:{port}?type={network}&encryption=none"

    # Проверяем Reality настройки (основной случай)
    if (
        hasattr(stream_settings, "reality_settings")
        and stream_settings.reality_settings
    ):
        settings = stream_settings.reality_settings.get("settings")
        if not settings:
            logger.warning("Reality settings not found in stream_settings")
            return None

        public_key = settings.get("publicKey")
        fp = settings.get("fingerprint")
        server_names = stream_settings.reality_settings.get("serverNames")
        short_ids = stream_settings.reality_settings.get("shortIds")

        logger.debug(
            f"Reality params - public_key: {bool(public_key)}, server_names: {bool(server_names)}, short_ids: {bool(short_ids)}"
        )

        if not all([public_key, server_names, short_ids]):
            logger.warning("Missing required Reality parameters")
            return None

        short_id = short_ids[0]
        server_name = server_names[0]
        pqv = settings.get("mldsa65Verify") or ""

        # Determine flow
        # XTLS-Vision flow is only valid for TCP + TLS/Reality
        flow_param = ""
        if network == "tcp":
            flow_param = "&flow=xtls-rprx-vision"

        if network == "grpc":
            # Extract grpc serviceName if available
            service_name = ""
            if hasattr(stream_settings, "grpc_settings"):
                grpc_settings = stream_settings.grpc_settings
                if isinstance(grpc_settings, dict):
                    service_name = grpc_settings.get("serviceName", "")
                elif hasattr(grpc_settings, "service_name"):  # Try object attribute
                    service_name = grpc_settings.service_name

            if service_name:
                base_link += f"&serviceName={service_name}"

            # gRPC usually works with mode=gun or multi
            base_link += "&mode=gun"

        connection_string = (
            f"{base_link}"
            f"&security=reality&pbk={public_key}&fp={fp}&sni={server_name}"
            f"&sid={short_id}&spx=%2F"
        )
        if pqv:
            connection_string += f"&pqv={quote(str(pqv), safe='')}"
        connection_string += f"{flow_param}#{remark}"
        logger.debug(
            "Generated Reality connection string for %s on %s", user_uuid, hostname
        )
        return connection_string

    # Проверяем TLS настройки
    elif hasattr(stream_settings, "tls_settings") and stream_settings.tls_settings:
        tls_settings = stream_settings.tls_settings.get("settings", {})
        server_name = tls_settings.get("serverName", hostname)
        fp = tls_settings.get("fingerprint", "chrome")

        if network == "grpc":
            # Extract grpc serviceName
            service_name = ""
            if hasattr(stream_settings, "grpc_settings"):
                grpc_settings = stream_settings.grpc_settings
                if isinstance(grpc_settings, dict):
                    service_name = grpc_settings.get("serviceName", "")
                elif hasattr(grpc_settings, "service_name"):
                    service_name = grpc_settings.service_name

            if service_name:
                base_link += f"&serviceName={service_name}"
            base_link += "&mode=gun"

        connection_string = (
            f"{base_link}" f"&security=tls&sni={server_name}&fp={fp}#{remark}"
        )
        logger.debug(
            "Generated TLS connection string for %s on %s", user_uuid, hostname
        )
        return connection_string

    # Без безопасности
    else:
        connection_string = f"{base_link}&security=none#{remark}"
        logger.debug(
            "Generated plain connection string for %s on %s", user_uuid, hostname
        )
        return connection_string


# ... (VMess and Trojan functions remain similar but skipped for brevity as VLESS is focus) ...


def _get_vmess_connection_string(
    inbound: Inbound, user_uuid: str, hostname: str, port: int, remark: str
) -> str | None:
    """Generate VMess connection string"""
    # Placeholder - VMess implementation isn't changing in this task
    logger.warning("VMess protocol not fully implemented yet")
    return None


def _get_trojan_connection_string(
    inbound: Inbound, user_uuid: str, hostname: str, port: int, remark: str
) -> str | None:
    """Generate Trojan connection string"""
    # Placeholder
    logger.warning("Trojan protocol not fully implemented yet")
    return None


def update_or_create_client_on_panel(
    api: Api,
    inbound_id: int,
    email: str,
    days_to_add: int = 0,
    seconds_to_add: int | None = None,
    telegram_id: str = None,
    absolute_expiry_ms: int | None = None,
    preserve_longer_expiry: bool = True,
) -> tuple[str | None, int | None]:
    def _is_record_not_found_error(exc: Exception) -> bool:
        return "record not found" in str(exc).lower()

    try:
        inbound_to_modify = _get_inbound_by_id_compat(api, inbound_id)
        if not inbound_to_modify:
            raise ValueError(f"Could not find inbound with ID {inbound_id}")

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []

        # Determine appropriate flow settings based on inbound config
        target_flow = ""
        is_tcp_reality_vision = False

        network, security = _get_stream_network_security(inbound_to_modify)
        protocol = (getattr(inbound_to_modify, "protocol", "") or "").lower()
        if network == "tcp" and security == "reality":
            target_flow = "xtls-rprx-vision"
            is_tcp_reality_vision = True

        logger.debug(
            f"Determined target flow for client: '{target_flow}' (is_reality_vision={is_tcp_reality_vision})"
        )

        client_index = -1
        for i, client in enumerate(inbound_to_modify.settings.clients):
            if client.email == email:
                client_index = i
                break

        if absolute_expiry_ms is not None:
            try:
                target_expiry_ms = int(absolute_expiry_ms)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Invalid absolute_expiry_ms value: {absolute_expiry_ms}"
                )
            if target_expiry_ms <= 0:
                raise ValueError(
                    f"absolute_expiry_ms must be positive, got: {target_expiry_ms}"
                )

            # Idempotent path for auto-provision by default:
            # never decrease expiry for existing clients, only move forward.
            # Admin-issued replacements may opt out and set the exact target expiry.
            if client_index != -1:
                if preserve_longer_expiry:
                    existing_client = inbound_to_modify.settings.clients[client_index]
                    current_ms = int(getattr(existing_client, "expiry_time", 0) or 0)
                    new_expiry_ms = max(current_ms, target_expiry_ms)
                else:
                    new_expiry_ms = target_expiry_ms
            else:
                new_expiry_ms = target_expiry_ms
        else:
            if seconds_to_add is not None:
                delta = timedelta(seconds=int(seconds_to_add))
            else:
                delta = timedelta(days=int(days_to_add))

            # Calculate expiry time for additive updates.
            if client_index != -1:
                existing_client = inbound_to_modify.settings.clients[client_index]
                if existing_client.expiry_time > time_utils.get_timestamp_ms(
                    time_utils.get_msk_now()
                ):
                    current_expiry_dt = time_utils.from_timestamp_ms(
                        existing_client.expiry_time
                    )
                    new_expiry_dt = current_expiry_dt + delta
                else:
                    new_expiry_dt = time_utils.get_msk_now() + delta
            else:
                new_expiry_dt = time_utils.get_msk_now() + delta

            new_expiry_ms = time_utils.get_timestamp_ms(new_expiry_dt)

        current_ts_ms = time_utils.get_timestamp_ms(time_utils.get_msk_now())
        should_enable_client = new_expiry_ms > current_ts_ms

        if _protocol_uses_raw_client_api(protocol):
            raw_clients = _get_raw_clients(api, inbound_id)
            raw_client = next(
                (client for client in raw_clients if client.get("email") == email),
                None,
            )
            if raw_client:
                raw_client["expiryTime"] = new_expiry_ms
                raw_client["enable"] = should_enable_client
                raw_client["totalGB"] = 0
                raw_client["reset"] = 0
                if telegram_id and not raw_client.get("tgId"):
                    raw_client["tgId"] = telegram_id
                client_uuid = str(raw_client.get("auth") or uuid.uuid4())
                raw_client["auth"] = client_uuid
                _update_raw_client(api, inbound_id, client_uuid, raw_client)
                logger.info(
                    "Updated existing Hysteria client '%s' on inbound %s",
                    email,
                    inbound_id,
                )
            else:
                raw_client, client_uuid = _build_raw_client_for_protocol(
                    protocol=protocol,
                    email=email,
                    enable=should_enable_client,
                    expiry_time=new_expiry_ms,
                    telegram_id=telegram_id,
                )
                _add_raw_client(api, inbound_id, raw_client)
                logger.info(
                    "Added new Hysteria client '%s' on inbound %s",
                    email,
                    inbound_id,
                )

            _normalize_client_traffic_state(api, inbound_id, email)
            return client_uuid, new_expiry_ms

        if client_index != -1:
            # Update existing client
            client_to_update = inbound_to_modify.settings.clients[client_index]
            previous_expiry_ms = int(getattr(client_to_update, "expiry_time", 0) or 0)
            previous_enabled = bool(getattr(client_to_update, "enable", True))
            client_to_update.expiry_time = new_expiry_ms
            client_to_update.enable = should_enable_client

            # Update flow ONLY if we determined a specific one is required (like Reality Vision)
            # Or if it's explicitly NOT vision anymore (e.g. switched to grpc) we might want to clear it?
            # Safer: explicitly set what we determined.
            client_to_update.flow = target_flow

            # Ensure all required parameters exist
            if not hasattr(client_to_update, "sub_id") or not client_to_update.sub_id:
                client_to_update.sub_id = uuid.uuid4().hex[:16]

            # Normalize to unlimited traffic for consistency across global hosts.
            # Otherwise legacy non-zero caps may cause "exhausted" on one host only.
            _set_unlimited_traffic_fields(client_to_update)

            if telegram_id and (
                not hasattr(client_to_update, "tg_id") or not client_to_update.tg_id
            ):
                client_to_update.tg_id = telegram_id

            client_uuid = _ensure_client_identifier_for_protocol(
                protocol, client_to_update, email
            )
            try:
                # Update the already-loaded inbound as the primary path.
                # This is more stable on panels that intermittently reject direct client.update
                # with "record not found" for otherwise valid existing clients.
                api.inbound.update(inbound_id, inbound_to_modify)
                logger.info(
                    f"Updated existing client '{email}' (UUID: {client_uuid}) on inbound {inbound_id}"
                )
            except Exception as inbound_update_error:
                if not _is_record_not_found_error(inbound_update_error):
                    raise

                logger.warning(
                    f"Client '{email}' inbound.update failed with 'record not found' on inbound {inbound_id}. "
                    "Trying client.update fallback."
                )
                try:
                    _update_client_compat(api, client_uuid, client_to_update)
                    logger.info(
                        f"Updated existing client '{email}' (UUID: {client_uuid}) on inbound {inbound_id} "
                        "via client.update fallback."
                    )
                except Exception as update_error:
                    if not _is_record_not_found_error(update_error):
                        raise

                    # Panel can keep a stale reference in clients list; final fallback is safe recreate.
                    logger.warning(
                        f"Client '{email}' client.update fallback also failed with 'record not found' "
                        f"on inbound {inbound_id}. Trying recreate fallback."
                    )
                    recreated_client, client_uuid = _build_client_for_inbound(
                        inbound=inbound_to_modify,
                        email=email,
                        enable=should_enable_client,
                        flow=target_flow,
                        expiry_time=new_expiry_ms,
                        telegram_id=telegram_id,
                    )
                    _add_client_compat(
                        api, inbound_id, recreated_client, client_uuid
                    )
                    actual_uuid = _get_raw_client_identifier_by_email(
                        api, inbound_id, protocol, email
                    )
                    if actual_uuid and actual_uuid != client_uuid:
                        logger.warning(
                            "Recreated client '%s' returned identifier '%s', but panel stores '%s'. Using panel identifier.",
                            email,
                            client_uuid,
                            actual_uuid,
                        )
                        client_uuid = actual_uuid
                    logger.info(
                        "Recreated client '%s' on inbound %s", email, inbound_id
                    )

            reactivated_from_expired = should_enable_client and (
                previous_expiry_ms <= current_ts_ms or not previous_enabled
            )
            # Reset traffic counters/state so 3x-ui no longer shows a stale
            # "exhausted" (исчерпано) badge after a renewal.
            _normalize_client_traffic_state(api, inbound_id, email)
            if reactivated_from_expired:
                _refresh_reactivated_client_visual_state(api, inbound_id, email)

        else:
            new_client, client_uuid = _build_client_for_inbound(
                inbound=inbound_to_modify,
                email=email,
                enable=should_enable_client,
                flow=target_flow,
                expiry_time=new_expiry_ms,
                telegram_id=telegram_id,
            )

            _add_client_compat(api, inbound_id, new_client, client_uuid)
            actual_uuid = _get_raw_client_identifier_by_email(
                api, inbound_id, protocol, email
            )
            if actual_uuid and actual_uuid != client_uuid:
                logger.warning(
                    "Added client '%s' returned identifier '%s', but panel stores '%s'. Using panel identifier.",
                    email,
                    client_uuid,
                    actual_uuid,
                )
                client_uuid = actual_uuid
            logger.info("Added new client '%s'", email)

        return client_uuid, new_expiry_ms

    except ValueError as ve:
        logger.error(f"Validation error in update_or_create_client_on_panel: {ve}")
        return None, None
    except ConnectionError as ce:
        logger.error(f"Network error in update_or_create_client_on_panel: {ce}")
        return None, None
    except Exception as e:
        logger.error(f"Error in update_or_create_client_on_panel: {e}", exc_info=True)
        return None, None


import asyncio


async def create_or_update_key_on_host(
    host_name: str, email: str, days_to_add: int, telegram_id: str = None
) -> Dict | None:
    return await asyncio.to_thread(
        _create_or_update_key_on_host_sync,
        host_name,
        email,
        days_to_add,
        None,
        telegram_id,
        None,
    )


async def create_or_update_key_on_host_seconds(
    host_name: str, email: str, seconds_to_add: int, telegram_id: str = None
) -> Dict | None:
    return await asyncio.to_thread(
        _create_or_update_key_on_host_sync,
        host_name,
        email,
        0,
        int(seconds_to_add),
        telegram_id,
        None,
    )


async def create_or_update_key_on_host_absolute_expiry(
    host_name: str,
    email: str,
    target_expiry_ms: int,
    telegram_id: str = None,
    preserve_longer_expiry: bool = True,
) -> Dict | None:
    return await asyncio.to_thread(
        _create_or_update_key_on_host_sync,
        host_name,
        email,
        0,
        None,
        telegram_id,
        int(target_expiry_ms),
        preserve_longer_expiry,
    )


def _create_or_update_key_on_host_sync(
    host_name: str,
    email: str,
    days_to_add: int,
    seconds_to_add: int | None,
    telegram_id: str = None,
    absolute_expiry_ms: int | None = None,
    preserve_longer_expiry: bool = True,
) -> Dict | None:
    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Workflow failed: Host '{host_name}' not found in the database.")
        return None

    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=host_data.get("api_token"),
    )
    if not api or not inbound:
        logger.error(
            f"Workflow failed: Could not log in or find inbound on host '{host_name}'."
        )
        return None

    client_uuid, new_expiry_ms = update_or_create_client_on_panel(
        api,
        inbound.id,
        email,
        days_to_add=days_to_add,
        seconds_to_add=seconds_to_add,
        telegram_id=telegram_id,
        absolute_expiry_ms=absolute_expiry_ms,
        preserve_longer_expiry=preserve_longer_expiry,
    )
    if not client_uuid:
        logger.error(
            f"Workflow failed: Could not create/update client '{email}' on host '{host_name}'."
        )
        return None

    server_remark = _build_server_remark(host_name)
    connection_string = _connection_string_for_client(
        api=api,
        inbound=inbound,
        host_url=host_data["host_url"],
        email=email,
        client_identifier=client_uuid,
        remark=server_remark,
    )

    logger.info(f"Successfully processed key for '{email}' on host '{host_name}'.")

    return {
        "client_uuid": client_uuid,
        "email": email,
        "expiry_timestamp_ms": new_expiry_ms,
        "connection_string": connection_string,
        "host_name": host_name,
    }


async def get_key_details_from_host(key_data: dict) -> dict | None:
    return await asyncio.to_thread(_get_key_details_from_host_sync, key_data)


def _get_key_details_from_host_sync(key_data: dict) -> dict | None:
    host_name = key_data.get("host_name")
    if not host_name:
        logger.error(
            f"Could not get key details: host_name is missing for key_id {key_data.get('key_id')}"
        )
        return None

    host_db_data = get_host(host_name)
    if not host_db_data:
        logger.error(
            f"Could not get key details: Host '{host_name}' not found in the database."
        )
        return None

    api, inbound = login_to_host(
        host_url=host_db_data["host_url"],
        username=host_db_data["host_username"],
        password=host_db_data["host_pass"],
        inbound_id=host_db_data["host_inbound_id"],
        api_token=host_db_data.get("api_token"),
    )
    if not api or not inbound:
        return None

    server_remark = _build_server_remark(host_name)
    connection_string = _connection_string_for_client(
        api=api,
        inbound=inbound,
        host_url=host_db_data["host_url"],
        email=key_data["key_email"],
        client_identifier=key_data["xui_client_uuid"],
        remark=server_remark,
    )
    return {"connection_string": connection_string}


async def get_client_traffic(key_data: dict) -> dict | None:
    return await asyncio.to_thread(_get_client_traffic_sync, key_data)


def _get_client_traffic_sync(key_data: dict) -> dict | None:
    host_name = key_data.get("host_name")
    if not host_name:
        return None

    host_db_data = get_host(host_name)
    if not host_db_data:
        return None

    api, inbound = login_to_host(
        host_url=host_db_data["host_url"],
        username=host_db_data["host_username"],
        password=host_db_data["host_pass"],
        inbound_id=host_db_data["host_inbound_id"],
        api_token=host_db_data.get("api_token"),
    )
    if not api or not inbound or not inbound.settings.clients:
        return None

    target_uuid = key_data.get("xui_client_uuid")
    protocol = getattr(inbound, "protocol", "") or ""
    for client in inbound.settings.clients:
        if _get_client_identifier_for_protocol(protocol, client) == target_uuid:
            return {
                "up": client.up,
                "down": client.down,
                "total": client.total,
                "expiry_time": client.expiry_time,
            }
    return None


async def get_connection_strings_for_host(host_name: str) -> dict[str, str]:
    return await asyncio.to_thread(_get_connection_strings_for_host_sync, host_name)


def _get_connection_strings_for_host_sync(host_name: str) -> dict[str, str]:
    host_db_data = get_host(host_name)
    if not host_db_data:
        logger.error(
            f"Could not get connection strings: Host '{host_name}' not found in the database."
        )
        return {}

    api, inbound = login_to_host(
        host_url=host_db_data["host_url"],
        username=host_db_data["host_username"],
        password=host_db_data["host_pass"],
        inbound_id=host_db_data["host_inbound_id"],
        api_token=host_db_data.get("api_token"),
    )
    if not api or not inbound:
        return {}

    inbound_fresh = _get_inbound_by_id_compat(api, inbound.id)
    if not inbound_fresh or not inbound_fresh.settings.clients:
        return {}

    server_remark = _build_server_remark(host_name)

    result: dict[str, str] = {}
    protocol = getattr(inbound_fresh, "protocol", "") or ""
    if _protocol_uses_raw_client_api(protocol):
        for raw_client in _get_raw_clients(api, inbound.id):
            email = raw_client.get("email")
            client_identifier = raw_client.get("auth")
            if not email or not client_identifier:
                continue
            conn = _connection_string_for_client(
                api=api,
                inbound=inbound_fresh,
                host_url=host_db_data["host_url"],
                email=email,
                client_identifier=client_identifier,
                remark=server_remark,
            )
            if conn:
                result[email] = conn
        return result

    for client in inbound_fresh.settings.clients:
        email = getattr(client, "email", None)
        if not email:
            continue

        client_identifier = _get_client_identifier_for_protocol(protocol, client)
        conn = None
        if client_identifier:
            conn = _connection_string_for_client(
                api=api,
                inbound=inbound_fresh,
                host_url=host_db_data["host_url"],
                email=email,
                client_identifier=client_identifier,
                remark=server_remark,
            )
        if conn:
            result[email] = conn

    return result


async def fix_client_parameters_on_host(host_name: str, client_email: str) -> bool:
    """Fix flow and encryption parameters for existing client on host"""
    return await asyncio.to_thread(
        _fix_client_parameters_on_host_sync, host_name, client_email
    )


def _fix_client_parameters_on_host_sync(host_name: str, client_email: str) -> bool:
    """Sync version of fix_client_parameters_on_host"""
    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Cannot fix client: Host '{host_name}' not found.")
        return False

    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=host_data.get("api_token"),
    )

    if not api or not inbound:
        logger.error(
            f"Cannot fix client: Login or inbound lookup failed for host '{host_name}'."
        )
        return False

    try:
        inbound_to_modify = _get_inbound_by_id_compat(api, inbound.id)
        if not inbound_to_modify:
            raise ValueError(f"Could not find inbound with ID {inbound.id}")

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []

        client_index = -1
        for i, client in enumerate(inbound_to_modify.settings.clients):
            if client.email == client_email:
                client_index = i
                break

        if client_index == -1:
            logger.warning(f"Client '{client_email}' not found on host '{host_name}'.")
            return False

        protocol = (getattr(inbound_to_modify, "protocol", "") or "").lower()
        if protocol != "vless":
            logger.info(
                "Skipping parameter fix for client '%s' on host '%s': protocol '%s' does not use VLESS flow fields.",
                client_email,
                host_name,
                protocol,
            )
            return True

        # Determine correct flow
        target_flow = ""
        network, security = _get_stream_network_security(inbound_to_modify)
        if network == "tcp" and security == "reality":
            target_flow = "xtls-rprx-vision"

        # Fix client parameters
        inbound_to_modify.settings.clients[client_index].flow = target_flow
        _set_unlimited_traffic_fields(inbound_to_modify.settings.clients[client_index])
        try:
            inbound_to_modify.settings.clients[client_index].encryption = "none"
        except (ValueError, AttributeError):
            pass  # Field might not exist in some library versions, skip it

        api.inbound.update(inbound.id, inbound_to_modify)

        logger.info(
            f"Successfully fixed parameters for client '{client_email}' on host '{host_name}'."
        )
        return True

    except Exception as e:
        logger.error(
            f"Failed to fix client '{client_email}' on host '{host_name}': {e}",
            exc_info=True,
        )
        return False


async def fix_all_client_parameters_on_host(host_name: str) -> int:
    return await asyncio.to_thread(_fix_all_client_parameters_on_host_sync, host_name)


def _fix_all_client_parameters_on_host_sync(host_name: str) -> int:
    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Cannot fix clients: Host '{host_name}' not found.")
        return 0

    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=host_data.get("api_token"),
    )

    if not api or not inbound:
        logger.error(
            f"Cannot fix clients: Login or inbound lookup failed for host '{host_name}'."
        )
        return 0

    try:
        keys_in_db = get_keys_for_host(host_name)
        now = time_utils.get_msk_now()

        # Fetch inbound once to detect missing clients
        inbound_to_modify = _get_inbound_by_id_compat(api, inbound.id)
        if not inbound_to_modify:
            raise ValueError(f"Could not find inbound with ID {inbound.id}")

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []

        existing_emails = {
            c.email
            for c in inbound_to_modify.settings.clients
            if getattr(c, "email", None)
        }

        # Ensure all DB keys exist on panel (recreate if missing only)
        for key in keys_in_db:
            email = key.get("key_email")
            expiry_str = key.get("expiry_date")
            if not email or not expiry_str:
                continue

            if email in existing_emails:
                continue

            expiry_dt = time_utils.parse_iso_to_msk(expiry_str)
            if not expiry_dt or expiry_dt <= now:
                continue

            remaining_seconds = int((expiry_dt - now).total_seconds())
            if remaining_seconds <= 0:
                continue

            try:
                client_uuid, new_expiry_ms = update_or_create_client_on_panel(
                    api,
                    inbound.id,
                    email,
                    days_to_add=0,
                    seconds_to_add=remaining_seconds,
                    telegram_id=None,
                )
                if client_uuid and new_expiry_ms:
                    server_remark = _build_server_remark(host_name)
                    conn = _connection_string_for_client(
                        api=api,
                        inbound=inbound,
                        host_url=host_data["host_url"],
                        email=email,
                        client_identifier=client_uuid,
                        remark=server_remark,
                    )
                    update_key_by_email(
                        key_email=email,
                        host_name=host_name,
                        xui_client_uuid=client_uuid,
                        expiry_timestamp_ms=new_expiry_ms,
                        connection_string=conn,
                        plan_id=key.get("plan_id"),
                    )
                    existing_emails.add(email)
                time.sleep(0.2)
            except Exception as e:
                logger.error(
                    f"Failed to ensure client '{email}' on host '{host_name}': {e}",
                    exc_info=True,
                )

        # Refresh inbound after potential additions and fix parameters in bulk
        inbound_to_modify = _get_inbound_by_id_compat(api, inbound.id)
        if not inbound_to_modify:
            raise ValueError(f"Could not find inbound with ID {inbound.id}")

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []

        protocol = (getattr(inbound_to_modify, "protocol", "") or "").lower()
        server_remark = _build_server_remark(host_name)

        if protocol != "vless":
            refreshed = 0
            if protocol in {"hysteria", "hysteria2"}:
                raw_clients = _get_raw_clients(api, inbound.id)
                client_rows = (
                    (client.get("email"), client.get("auth"))
                    for client in raw_clients
                )
            else:
                client_rows = (
                    (
                        getattr(client, "email", None),
                        _get_client_identifier_for_protocol(protocol, client),
                    )
                    for client in inbound_to_modify.settings.clients
                )

            for email, client_identifier in client_rows:
                if not email or not client_identifier:
                    continue
                key = get_key_by_email(email)
                if not key:
                    continue
                conn = _connection_string_for_client(
                    api=api,
                    inbound=inbound_to_modify,
                    host_url=host_data["host_url"],
                    email=email,
                    client_identifier=str(client_identifier),
                    remark=server_remark,
                )
                if conn:
                    update_key_connection_string(key["key_id"], conn)
                    purge_missing_key(email)
                    refreshed += 1

            logger.info(
                "Refreshed %s connection string(s) for protocol '%s' on host '%s' without inbound.update.",
                refreshed,
                protocol,
                host_name,
            )
            return refreshed

        network, security = _get_stream_network_security(inbound_to_modify)
        target_flow = ""
        if network == "tcp" and security == "reality":
            target_flow = "xtls-rprx-vision"

        updated = 0
        for client in inbound_to_modify.settings.clients:
            client.flow = target_flow
            _set_unlimited_traffic_fields(client)
            try:
                client.encryption = "none"
            except (ValueError, AttributeError):
                pass
            updated += 1

            try:
                email = getattr(client, "email", None)
                if not email:
                    continue
                key = get_key_by_email(email)
                if not key:
                    continue
                client_identifier = _get_client_identifier_for_protocol(
                    getattr(inbound_to_modify, "protocol", "") or "",
                    client,
                )
                conn = _connection_string_for_client(
                    api=api,
                    inbound=inbound_to_modify,
                    host_url=host_data["host_url"],
                    email=email,
                    client_identifier=client_identifier,
                    remark=server_remark,
                )
                if conn:
                    update_key_connection_string(key["key_id"], conn)
                    purge_missing_key(email)
            except Exception as e:
                logger.warning(
                    f"Failed to refresh connection string for '{getattr(client, 'email', '')}': {e}"
                )

        api.inbound.update(inbound.id, inbound_to_modify)
        logger.info(f"Fixed parameters for {updated} clients on host '{host_name}'.")
        return updated

    except Exception as e:
        logger.error(f"Failed to fix clients on host '{host_name}': {e}", exc_info=True)
        return 0


async def sync_clients_state_on_host(
    host_name: str, desired_by_email: dict[str, dict]
) -> dict:
    return await asyncio.to_thread(
        _sync_clients_state_on_host_sync, host_name, desired_by_email
    )


def _sync_clients_state_on_host_sync(
    host_name: str, desired_by_email: dict[str, dict]
) -> dict:
    """
    Synchronize clients on one host to the target state from DB:
    - enable/disable status
    - expiry timestamp
    - unlimited traffic fields
    """
    result = {
        "host": host_name,
        "checked": 0,
        "updated": 0,
        "already_ok": 0,
        "not_found": 0,
        "traffic_fixed": 0,
        "errors": 0,
    }

    if not desired_by_email:
        return result

    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Cannot sync clients state: Host '{host_name}' not found.")
        result["errors"] += 1
        return result

    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=host_data.get("api_token"),
    )

    if not api or not inbound:
        logger.error(
            f"Cannot sync clients state: Login or inbound lookup failed for host '{host_name}'."
        )
        result["errors"] += 1
        return result

    try:
        inbound_to_modify = _get_inbound_by_id_compat(api, inbound.id)
        if not inbound_to_modify:
            raise ValueError(f"Could not find inbound with ID {inbound.id}")

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []

        protocol = (getattr(inbound_to_modify, "protocol", "") or "").lower()
        if _protocol_uses_raw_client_api(protocol):
            raw_clients = _get_raw_clients(api, inbound.id)
            raw_clients_by_email = {
                str(client.get("email")): client
                for client in raw_clients
                if client.get("email")
            }

            for email, state in desired_by_email.items():
                result["checked"] += 1
                raw_client = raw_clients_by_email.get(email)
                if not raw_client:
                    result["not_found"] += 1
                    continue

                changed = False
                target_enabled = bool(state.get("enabled", True))
                if bool(raw_client.get("enable", True)) != target_enabled:
                    raw_client["enable"] = target_enabled
                    changed = True

                target_expiry_ms = state.get("expiry_timestamp_ms")
                if target_expiry_ms is not None:
                    try:
                        target_expiry_ms = int(target_expiry_ms)
                        current_expiry_ms = int(raw_client.get("expiryTime") or 0)
                        if abs(current_expiry_ms - target_expiry_ms) > 1000:
                            raw_client["expiryTime"] = target_expiry_ms
                            changed = True
                    except Exception:
                        result["errors"] += 1

                if state.get("force_unlimited", False):
                    if raw_client.get("totalGB", 0) != 0:
                        raw_client["totalGB"] = 0
                        changed = True
                    if raw_client.get("reset", 0) != 0:
                        raw_client["reset"] = 0
                        changed = True

                client_identifier = raw_client.get("auth")
                if not client_identifier:
                    client_identifier = str(uuid.uuid4())
                    raw_client["auth"] = client_identifier
                    changed = True

                if changed:
                    _update_raw_client(api, inbound.id, str(client_identifier), raw_client)
                    result["updated"] += 1
                    _normalize_client_traffic_state(api, inbound.id, email)
                else:
                    result["already_ok"] += 1

            return result

        clients_by_email = {
            c.email: c
            for c in inbound_to_modify.settings.clients
            if getattr(c, "email", None)
        }

        any_changed = False
        # Track clients whose clientTraffics row must be normalized after the
        # inbound update. In 3x-ui the panel can leave clientTraffics.enable=0
        # after auto-expiry/traffic events, which renders as "исчерпано" even when
        # DB state says the account should simply be disabled or active again.
        #
        # We therefore reset stats in two cases:
        # - target_enabled=True and the record changed (renew, re-enable, cap fix)
        # - target_enabled=False and we are disabling the client from DB state
        #
        # resetClientTraffic flips that stale traffic-state flag back to enabled in
        # clientTraffics, while the real access state remains controlled by
        # client.enable from inbound.update.
        emails_to_reset_stats: set[str] = set()
        reactivated_emails: set[str] = set()
        now_ms = time_utils.get_timestamp_ms(time_utils.get_msk_now())

        for email, state in desired_by_email.items():
            result["checked"] += 1
            client = clients_by_email.get(email)
            if not client:
                result["not_found"] += 1
                continue

            changed = False

            target_enabled = bool(state.get("enabled", True))
            was_enabled = bool(getattr(client, "enable", True))
            current_expiry_ms = int(getattr(client, "expiry_time", 0) or 0)
            enable_state_changed = was_enabled != target_enabled
            if enable_state_changed:
                client.enable = target_enabled
                changed = True

            target_expiry_ms = state.get("expiry_timestamp_ms")
            if target_expiry_ms is not None:
                try:
                    target_expiry_ms = int(target_expiry_ms)
                    if abs(current_expiry_ms - target_expiry_ms) > 1000:
                        client.expiry_time = target_expiry_ms
                        changed = True
                except Exception:
                    result["errors"] += 1

            if state.get("force_unlimited", False):
                if _set_unlimited_traffic_fields(client):
                    changed = True

            if target_enabled:
                # Renewals can keep client.enable=true while only expiry/caps change.
                # In that case 3x-ui may still show a stale exhausted badge until
                # resetClientTraffic is called explicitly.
                if changed:
                    emails_to_reset_stats.add(email)
                    if current_expiry_ms <= now_ms or not was_enabled:
                        reactivated_emails.add(email)
                elif _client_traffic_is_disabled(api, email):
                    logger.info(
                        "Client '%s' on host '%s' is active in DB but disabled/exhausted in clientTraffics; refreshing.",
                        email,
                        host_name,
                    )
                    emails_to_reset_stats.add(email)
                    reactivated_emails.add(email)
                    result["traffic_fixed"] += 1
            elif enable_state_changed:
                # When DB marks a key expired we want the host to show it as disabled,
                # not as traffic-exhausted due to a stale clientTraffics flag.
                emails_to_reset_stats.add(email)

            if changed:
                result["updated"] += 1
                any_changed = True
            else:
                result["already_ok"] += 1

        if any_changed:
            api.inbound.update(inbound.id, inbound_to_modify)

        # Reset traffic stats after inbound.update so 3x-ui clears stale
        # "исчерпано" state from clientTraffics for both renewals and expiries.
        for email in emails_to_reset_stats:
            _normalize_client_traffic_state(api, inbound.id, email)

        for email in reactivated_emails:
            _refresh_reactivated_client_visual_state(api, inbound.id, email)

        return result

    except Exception as e:
        logger.error(
            f"Failed to sync clients state on host '{host_name}': {e}", exc_info=True
        )
        result["errors"] += 1
        return result


async def delete_client_on_host(host_name: str, client_email: str) -> bool:
    return await asyncio.to_thread(_delete_client_on_host_sync, host_name, client_email)


def _delete_client_on_host_sync(host_name: str, client_email: str) -> bool:
    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Cannot delete client: Host '{host_name}' not found.")
        return False

    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=host_data.get("api_token"),
    )

    if not api or not inbound:
        logger.error(
            f"Cannot delete client: Login or inbound lookup failed for host '{host_name}'."
        )
        return False

    try:
        client_to_delete = get_key_by_email(client_email)
        if not client_to_delete:
            logger.warning(
                f"Client '{client_email}' not found in local database for host '{host_name}' (already deleted or out of sync)."
            )
            return True

        _delete_client_compat(
            api,
            inbound.id,
            client_to_delete["xui_client_uuid"],
            client_email,
        )
        logger.info(
            f"Successfully deleted client '{client_to_delete['xui_client_uuid']}' from host '{host_name}'."
        )
        return True

    except Exception as e:
        logger.error(
            f"Failed to delete client '{client_email}' from host '{host_name}': {e}",
            exc_info=True,
        )
        return False


async def sync_inbounds_xtls_from_all_hosts() -> dict[str, dict]:
    """
    Synchronize XTLS settings across all hosts.

    For each host and inbound:
    - Determine protocol type (Reality TCP, gRPC, etc.)
    - Validate XTLS settings match protocol requirements
    - Auto-fix if mismatch detected
    - Report results

    Runs at startup and periodically in background (every 5-10 min).

    Returns: dict with sync results for each host
    """
    from shop_bot.data_manager.database import get_all_hosts

    all_hosts = get_all_hosts(only_enabled=True)
    if not all_hosts:
        logger.warning("No hosts configured in database. XTLS sync skipped.")
        return {"status": "no_hosts"}

    results = {}

    for host_info in all_hosts:
        host_name = host_info.get("host_name")
        logger.info(f"Starting XTLS sync for host: {host_name}")
        results[host_name] = _sync_xtls_for_host(host_info)

    return results


def _sync_xtls_for_host(host_info: dict) -> dict:
    """
    Synchronize XTLS settings for a single host.

    Returns: dict with sync result for the host
    """
    host_name = host_info.get("host_name")
    try:
        # Login to host
        api, inbound = login_to_host(
            host_url=host_info["host_url"],
            username=host_info["host_username"],
            password=host_info["host_pass"],
            inbound_id=host_info["host_inbound_id"],
            api_token=host_info.get("api_token"),
        )

        if not api or not inbound:
            logger.error(f"Could not connect to host '{host_name}' for XTLS sync")
            return {"status": "connection_failed", "fixed": 0}

        # Get fresh inbound data
        inbound_fresh = _get_inbound_by_id_compat(api, inbound.id)
        if not inbound_fresh or not inbound_fresh.settings.clients:
            logger.warning(f"No clients found on host '{host_name}'")
            return {"status": "no_clients", "fixed": 0}

        # Determine inbound protocol type
        protocol = getattr(inbound_fresh, "protocol", "unknown").lower()
        network, security = _get_stream_network_security(inbound_fresh)

        logger.info(
            f"Host '{host_name}' - protocol: {protocol}, network: {network}, security: {security}"
        )

        # Validate and fix XTLS for each client
        fixed_count = 0
        issues_found = []

        for client in inbound_fresh.settings.clients:
            client_email = client.email
            client_flow = getattr(client, "flow", "") or ""

            # Determine expected XTLS config
            expected_flow = ""
            expected_security = "none"

            if protocol == "vless":
                if network == "tcp" and security == "reality":
                    expected_flow = "xtls-rprx-vision"
                    expected_security = "reality"
                elif network == "tcp" and security == "tls":
                    expected_security = "tls"
                elif network == "grpc":
                    # gRPC doesn't use XTLS flow
                    expected_security = security

            # Check if fix needed
            needs_fix = False
            fix_reason = ""

            if protocol == "vless" and network == "tcp" and security == "reality":
                # Reality TCP MUST have XTLS flow
                if client_flow != "xtls-rprx-vision":
                    needs_fix = True
                    fix_reason = f"Flow '{client_flow}' != 'xtls-rprx-vision' (Reality TCP requires XTLS-Vision)"
            elif protocol == "vless" and network == "grpc":
                # gRPC should not have XTLS flow
                if "xtls" in client_flow.lower():
                    needs_fix = True
                    fix_reason = f"Flow contains XTLS ('{client_flow}') but gRPC doesn't use XTLS"

            if needs_fix:
                logger.info(f"Client '{client_email}' needs XTLS fix: {fix_reason}")
                issues_found.append(
                    {
                        "email": client_email,
                        "reason": fix_reason,
                        "current_flow": client_flow,
                        "expected_flow": expected_flow,
                    }
                )
                # Collect the fix in memory; apply a single API call after the loop.
                client.flow = expected_flow
                fixed_count += 1

        # Apply all collected XTLS fixes in one inbound update instead of one per client.
        if fixed_count > 0:
            try:
                api.inbound.update(inbound.id, inbound_fresh)
                logger.info(
                    f"Applied XTLS flow fix for {fixed_count} client(s) on inbound {inbound.id}"
                )
            except Exception as fix_error:
                logger.error(
                    f"Failed to apply XTLS fixes for host '{host_name}': {fix_error}"
                )
                fixed_count = 0

        result = {
            "status": "success",
            "fixed": fixed_count,
            "issues": issues_found,
            "protocol": protocol,
            "network": network,
            "security": security,
        }

        if fixed_count > 0:
            logger.info(
                f"XTLS sync completed for host '{host_name}': {fixed_count} clients fixed"
            )
        else:
            logger.debug(f"XTLS sync completed for host '{host_name}': all clients OK")

        return result

    except Exception as e:
        logger.error(f"XTLS sync failed for host '{host_name}': {e}", exc_info=True)
        return {"status": "error", "error": str(e), "fixed": 0}


def sync_inbounds_xtls_for_hosts(host_names: set[str]) -> dict[str, dict]:
    from shop_bot.data_manager.database import get_all_hosts

    if not host_names:
        return {}

    results = {}
    all_hosts = get_all_hosts(only_enabled=True)
    if not all_hosts:
        logger.warning("No hosts configured in database. XTLS sync skipped.")
        return {"status": "no_hosts"}

    selected_hosts = [h for h in all_hosts if h.get("host_name") in host_names]
    if not selected_hosts:
        logger.warning("Requested hosts not found in database. XTLS sync skipped.")
        return {"status": "no_matching_hosts"}

    for host_info in selected_hosts:
        host_name = host_info.get("host_name")
        logger.info(f"Starting XTLS sync for host: {host_name}")
        results[host_name] = _sync_xtls_for_host(host_info)

    return results
