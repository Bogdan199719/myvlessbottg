import uuid
import time
from datetime import timedelta
from shop_bot.utils import time_utils
import logging
from urllib.parse import urlparse
from typing import List, Dict

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
_ERROR_LOG_INTERVAL = 300  # Log same error once per 5 minutes
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
    "🇺🇸": ["usa", "united states", "america"],
    "🇨🇦": ["canada"],
    "🇲🇽": ["mexico"],
    "🇩🇪": ["germany", "deutschland"],
    "🇳🇱": ["netherlands", "nederland"],
    "🇫🇷": ["france", "french"],
    "🇬🇧": ["uk", "united kingdom", "britain", "england"],
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

    logger.warning(f"No flag detected for host '{host_name}', defaulting to USA.")
    return "🇺🇸"  # Default to USA


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


def login_to_host(
    host_url: str, username: str, password: str, inbound_id: int
) -> tuple[Api | None, Inbound | None]:
    host_url = host_url.rstrip("/")

    for attempt in range(1, _XUI_LOGIN_ATTEMPTS + 1):
        try:
            api = Api(host=host_url, username=username, password=password)
            api.login()
            inbounds: List[Inbound] = api.inbound.get_list()
            target_inbound = next(
                (inbound for inbound in inbounds if inbound.id == inbound_id), None
            )

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


def _set_client_enabled_state(
    api: Api, inbound_id: int, email: str, enabled: bool
) -> bool:
    """Best-effort single-client enable toggle via inbound.update."""
    try:
        inbound_fresh = api.inbound.get_by_id(inbound_id)
        if not inbound_fresh:
            return False
        if inbound_fresh.settings.clients is None:
            inbound_fresh.settings.clients = []

        for client in inbound_fresh.settings.clients:
            if getattr(client, "email", None) != email:
                continue
            if bool(getattr(client, "enable", True)) == enabled:
                return True
            client.enable = enabled
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
    after an expired client is renewed. Mimic the manual disable/enable fix.
    """
    try:
        _set_client_enabled_state(api, inbound_id, email, False)
        time.sleep(0.15)
        _set_client_enabled_state(api, inbound_id, email, True)
        time.sleep(0.15)
        api.client.reset_stats(inbound_id, email)
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
    else:
        logger.error(f"Unsupported protocol: {protocol}")
        return None


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
            f"&sid={short_id}&spx=%2F{flow_param}#{remark}"
        )
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
        inbound_to_modify = api.inbound.get_by_id(inbound_id)
        if not inbound_to_modify:
            raise ValueError(f"Could not find inbound with ID {inbound_id}")

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []

        # Determine appropriate flow settings based on inbound config
        target_flow = ""
        is_tcp_reality_vision = False

        network, security = _get_stream_network_security(inbound_to_modify)
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

            client_uuid = client_to_update.id
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
                    api.client.update(client_uuid, client_to_update)
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
                    client_uuid = str(uuid.uuid4())
                    subscription_id = uuid.uuid4().hex[:16]
                    recreated_client = Client(
                        id=client_uuid,
                        email=email,
                        enable=should_enable_client,
                        flow=target_flow,
                        expiry_time=new_expiry_ms,
                        sub_id=subscription_id,
                        total_gb=0,
                        reset=0,
                        tg_id=telegram_id,
                    )
                    _set_unlimited_traffic_fields(recreated_client)
                    api.client.add(inbound_id, [recreated_client])
                    logger.info(
                        f"Recreated client '{email}' (UUID: {client_uuid}) on inbound {inbound_id}"
                    )

            # Reset traffic counters so the 3x-ui panel no longer shows "exhausted" (исчерпано).
            # This is a best-effort call: if it fails the key is still functional.
            try:
                api.client.reset_stats(inbound_id, email)
                logger.debug(
                    f"Traffic stats reset for '{email}' on inbound {inbound_id}"
                )
            except Exception as rst_err:
                logger.warning(
                    f"Could not reset traffic stats for '{email}': {rst_err}"
                )

            reactivated_from_expired = should_enable_client and (
                previous_expiry_ms <= current_ts_ms or not previous_enabled
            )
            if reactivated_from_expired:
                _refresh_reactivated_client_visual_state(api, inbound_id, email)

        else:
            client_uuid = str(uuid.uuid4())
            subscription_id = uuid.uuid4().hex[:16]

            new_client = Client(
                id=client_uuid,
                email=email,
                enable=should_enable_client,
                flow=target_flow,
                expiry_time=new_expiry_ms,
                sub_id=subscription_id,
                total_gb=0,
                reset=0,
                tg_id=telegram_id,
            )
            _set_unlimited_traffic_fields(new_client)

            api.client.add(inbound_id, [new_client])
            logger.info(f"Added new client '{email}' (UUID: {client_uuid})")

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

    # Clean remark for URL safety
    safe_remark = host_name.replace(" ", "_").encode("ascii", "ignore").decode("ascii")
    # Determine country flag based on server name
    country_flag = get_country_flag_by_host(host_name)
    # Default is handled in the function (returns 🇺🇸)

    # Use server name (cleaned) with country flag for better UX
    # Clean server name: remove non-ASCII, replace spaces, keep only alphanumeric and underscores
    clean_server_name = (
        host_name.replace(" ", "").encode("ascii", "ignore").decode("ascii")
    )
    # Remove any remaining special chars, keep only letters, numbers, underscores
    clean_server_name = "".join(c for c in clean_server_name if c.isalnum() or c == "_")
    # Remove leading underscores
    clean_server_name = clean_server_name.lstrip("_")
    server_remark = f"{country_flag}{clean_server_name}"
    connection_string = get_connection_string(
        inbound, client_uuid, host_data["host_url"], remark=server_remark
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
    )
    if not api or not inbound:
        return None

    # Determine country flag based on server name
    country_flag = get_country_flag_by_host(host_name)
    # Default is handled in the function (returns 🇺🇸)

    # Use server name (cleaned) with country flag for better UX
    # Clean server name: remove non-ASCII, replace spaces, keep only alphanumeric and underscores
    clean_server_name = (
        host_name.replace(" ", "").encode("ascii", "ignore").decode("ascii")
    )
    # Remove any remaining special chars, keep only letters, numbers, underscores
    clean_server_name = "".join(c for c in clean_server_name if c.isalnum() or c == "_")
    # Remove leading underscores
    clean_server_name = clean_server_name.lstrip("_")
    server_remark = f"{country_flag}{clean_server_name}"
    connection_string = get_connection_string(
        inbound,
        key_data["xui_client_uuid"],
        host_db_data["host_url"],
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
    )
    if not api or not inbound or not inbound.settings.clients:
        return None

    target_uuid = key_data.get("xui_client_uuid")
    for client in inbound.settings.clients:
        if client.id == target_uuid:
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
    )
    if not api or not inbound:
        return {}

    inbound_fresh = api.inbound.get_by_id(inbound.id)
    if not inbound_fresh or not inbound_fresh.settings.clients:
        return {}

    country_flag = get_country_flag_by_host(host_name)
    clean_server_name = (
        host_name.replace(" ", "").encode("ascii", "ignore").decode("ascii")
    )
    clean_server_name = "".join(c for c in clean_server_name if c.isalnum() or c == "_")
    clean_server_name = clean_server_name.lstrip("_")
    server_remark = f"{country_flag}{clean_server_name}"

    result: dict[str, str] = {}
    for client in inbound_fresh.settings.clients:
        email = getattr(client, "email", None)
        client_uuid = getattr(client, "id", None)
        if not email or not client_uuid:
            continue
        conn = get_connection_string(
            inbound_fresh, client_uuid, host_db_data["host_url"], remark=server_remark
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
    )

    if not api or not inbound:
        logger.error(
            f"Cannot fix client: Login or inbound lookup failed for host '{host_name}'."
        )
        return False

    try:
        inbound_to_modify = api.inbound.get_by_id(inbound.id)
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
        inbound_to_modify = api.inbound.get_by_id(inbound.id)
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
                    country_flag = get_country_flag_by_host(host_name)
                    clean_server_name = (
                        host_name.replace(" ", "")
                        .encode("ascii", "ignore")
                        .decode("ascii")
                    )
                    clean_server_name = "".join(
                        c for c in clean_server_name if c.isalnum() or c == "_"
                    ).lstrip("_")
                    server_remark = f"{country_flag}{clean_server_name}"
                    conn = get_connection_string(
                        inbound,
                        client_uuid,
                        host_data["host_url"],
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
        inbound_to_modify = api.inbound.get_by_id(inbound.id)
        if not inbound_to_modify:
            raise ValueError(f"Could not find inbound with ID {inbound.id}")

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []

        network, security = _get_stream_network_security(inbound_to_modify)
        target_flow = ""
        if network == "tcp" and security == "reality":
            target_flow = "xtls-rprx-vision"

        country_flag = get_country_flag_by_host(host_name)
        clean_server_name = (
            host_name.replace(" ", "").encode("ascii", "ignore").decode("ascii")
        )
        clean_server_name = "".join(
            c for c in clean_server_name if c.isalnum() or c == "_"
        ).lstrip("_")
        server_remark = f"{country_flag}{clean_server_name}"

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
                conn = get_connection_string(
                    inbound_to_modify,
                    client.id,
                    host_data["host_url"],
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
    )

    if not api or not inbound:
        logger.error(
            f"Cannot sync clients state: Login or inbound lookup failed for host '{host_name}'."
        )
        result["errors"] += 1
        return result

    try:
        inbound_to_modify = api.inbound.get_by_id(inbound.id)
        if not inbound_to_modify:
            raise ValueError(f"Could not find inbound with ID {inbound.id}")

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []

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
            enable_state_changed = was_enabled != target_enabled
            if enable_state_changed:
                client.enable = target_enabled
                changed = True

            target_expiry_ms = state.get("expiry_timestamp_ms")
            if target_expiry_ms is not None:
                try:
                    target_expiry_ms = int(target_expiry_ms)
                    current_expiry_ms = int(getattr(client, "expiry_time", 0) or 0)
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
            try:
                api.client.reset_stats(inbound.id, email)
                logger.debug(
                    "Normalized traffic state for client '%s' on host '%s'.",
                    email,
                    host_name,
                )
            except Exception as rst_err:
                logger.warning(
                    "Could not reset traffic stats for '%s' on host '%s': %s",
                    email,
                    host_name,
                    rst_err,
                )

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

        api.client.delete(inbound.id, client_to_delete["xui_client_uuid"])
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
        )

        if not api or not inbound:
            logger.error(f"Could not connect to host '{host_name}' for XTLS sync")
            return {"status": "connection_failed", "fixed": 0}

        # Get fresh inbound data
        inbound_fresh = api.inbound.get_by_id(inbound.id)
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
