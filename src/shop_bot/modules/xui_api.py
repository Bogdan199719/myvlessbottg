import uuid
from datetime import timedelta
from shop_bot.utils import time_utils
import logging
from urllib.parse import urlparse
from typing import List, Dict

from py3xui import Api, Client, Inbound

from shop_bot.data_manager.database import get_host, get_key_by_email

logger = logging.getLogger(__name__)

def login_to_host(host_url: str, username: str, password: str, inbound_id: int) -> tuple[Api | None, Inbound | None]:
    try:
        host_url = host_url.rstrip('/')
        api = Api(host=host_url, username=username, password=password)
        api.login()
        inbounds: List[Inbound] = api.inbound.get_list()
        target_inbound = next((inbound for inbound in inbounds if inbound.id == inbound_id), None)
        
        if target_inbound is None:
            logger.error(f"Inbound with ID '{inbound_id}' not found on host '{host_url}'")
            return api, None
        return api, target_inbound
    except ValueError as ve:
        logger.error(f"Configuration error for host '{host_url}': {ve}")
        return None, None
    except ConnectionError as ce:
        logger.error(f"Connection failed to host '{host_url}': {ce}")
        return None, None
    except Exception as e:
        logger.error(f"Login or inbound retrieval failed for host '{host_url}': {e}", exc_info=True)
        return None, None

def get_connection_string(inbound: Inbound, user_uuid: str, host_url: str, remark: str) -> str | None:
    if not inbound:
        logger.error("Inbound is None")
        return None

    parsed_url = urlparse(host_url)
    port = inbound.port
    protocol = getattr(inbound, 'protocol', 'unknown')

    # Special handling for Reality - use port 443 (standard HTTPS port)
    if hasattr(inbound, 'stream_settings') and inbound.stream_settings:
        stream_settings = inbound.stream_settings
        if hasattr(stream_settings, 'reality_settings') and stream_settings.reality_settings:
            # Reality always uses port 443 for client connections (HTTPS)
            port = 443
            logger.info(f"Using port 443 for Reality protocol instead of inbound port {inbound.port}")

    # Keep original remark (including Unicode flag)
    safe_remark = remark

    logger.info(f"Generating connection string - protocol: {protocol}, port: {port}, hostname: {parsed_url.hostname}, remark: {safe_remark}")

    # Определяем тип протокола
    protocol_lower = protocol.lower()

    if protocol_lower == "vless":
        return _get_vless_connection_string(inbound, user_uuid, parsed_url.hostname, port, safe_remark)
    elif protocol_lower == "vmess":
        return _get_vmess_connection_string(inbound, user_uuid, parsed_url.hostname, port, safe_remark)
    elif protocol_lower == "trojan":
        return _get_trojan_connection_string(inbound, user_uuid, parsed_url.hostname, port, safe_remark)
    else:
        logger.error(f"Unsupported protocol: {protocol}")
        return None

def _get_vless_connection_string(inbound: Inbound, user_uuid: str, hostname: str, port: int, remark: str) -> str | None:
    """Generate VLESS connection string with automatic parameter detection"""

    stream_settings = inbound.stream_settings
    logger.info(f"Generating VLESS connection string for inbound protocol: {getattr(inbound, 'protocol', 'unknown')}, port: {port}")

    # Проверяем Reality настройки (основной случай)
    if hasattr(stream_settings, 'reality_settings') and stream_settings.reality_settings:
        settings = stream_settings.reality_settings.get("settings")
        if not settings:
            logger.warning("Reality settings not found in stream_settings")
            return None

        public_key = settings.get("publicKey")
        fp = settings.get("fingerprint")
        server_names = stream_settings.reality_settings.get("serverNames")
        short_ids = stream_settings.reality_settings.get("shortIds")

        logger.info(f"Reality params - public_key: {bool(public_key)}, server_names: {bool(server_names)}, short_ids: {bool(short_ids)}")

        if not all([public_key, server_names, short_ids]):
            logger.warning("Missing required Reality parameters")
            return None

        short_id = short_ids[0]
        server_name = server_names[0]

        connection_string = (
            f"vless://{user_uuid}@{hostname}:{port}"
            f"?type=tcp&encryption=none&security=reality&pbk={public_key}&fp={fp}&sni={server_name}"
            f"&sid={short_id}&spx=%2F&flow=xtls-rprx-vision#{remark}"
        )
        logger.info(f"Generated Reality connection string: {connection_string}")
        return connection_string

    # Проверяем TLS настройки
    elif hasattr(stream_settings, 'tls_settings') and stream_settings.tls_settings:
        tls_settings = stream_settings.tls_settings.get("settings", {})
        server_name = tls_settings.get("serverName", hostname)
        fp = tls_settings.get("fingerprint", "chrome")

        connection_string = (
            f"vless://{user_uuid}@{hostname}:{port}"
            f"?type=tcp&encryption=none&security=tls&sni={server_name}&fp={fp}#{remark}"
        )
        logger.info(f"Generated TLS connection string: {connection_string}")
        return connection_string

    # Без безопасности
    else:
        connection_string = f"vless://{user_uuid}@{hostname}:{port}?type=tcp&encryption=none&security=none#{remark}"
        logger.info(f"Generated plain connection string: {connection_string}")
        return connection_string

def _get_vmess_connection_string(inbound: Inbound, user_uuid: str, hostname: str, port: int, remark: str) -> str | None:
    """Generate VMess connection string"""
    # Аналогичная логика для VMess
    logger.warning("VMess protocol not fully implemented yet")
    return None

def _get_trojan_connection_string(inbound: Inbound, user_uuid: str, hostname: str, port: int, remark: str) -> str | None:
    """Generate Trojan connection string"""
    # Аналогичная логика для Trojan
    logger.warning("Trojan protocol not fully implemented yet")
    return None

def update_or_create_client_on_panel(api: Api, inbound_id: int, email: str, days_to_add: int = 0, seconds_to_add: int | None = None, telegram_id: str = None) -> tuple[str | None, int | None]:
    try:
        inbound_to_modify = api.inbound.get_by_id(inbound_id)
        if not inbound_to_modify:
            raise ValueError(f"Could not find inbound with ID {inbound_id}")

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []
            
        client_index = -1
        for i, client in enumerate(inbound_to_modify.settings.clients):
            if client.email == email:
                client_index = i
                break

        if seconds_to_add is not None:
            delta = timedelta(seconds=int(seconds_to_add))
        else:
            delta = timedelta(days=int(days_to_add))
        
        # Calculate expiry time
        if client_index != -1:
            existing_client = inbound_to_modify.settings.clients[client_index]
            if existing_client.expiry_time > time_utils.get_timestamp_ms(time_utils.get_msk_now()):
                current_expiry_dt = time_utils.from_timestamp_ms(existing_client.expiry_time)
                new_expiry_dt = current_expiry_dt + delta
            else:
                new_expiry_dt = time_utils.get_msk_now() + delta
        else:
            new_expiry_dt = time_utils.get_msk_now() + delta

        new_expiry_ms = time_utils.get_timestamp_ms(new_expiry_dt)
        
        if client_index != -1:
            # Update existing client
            client_to_update = inbound_to_modify.settings.clients[client_index]
            client_to_update.expiry_time = new_expiry_ms
            client_to_update.enable = True
            client_to_update.flow = "xtls-rprx-vision"
            
            # Ensure all required parameters exist
            if not hasattr(client_to_update, 'sub_id') or not client_to_update.sub_id:
                client_to_update.sub_id = uuid.uuid4().hex[:16]

            if not hasattr(client_to_update, 'total_gb') or client_to_update.total_gb is None:
                client_to_update.total_gb = 0

            if not hasattr(client_to_update, 'reset') or client_to_update.reset is None:
                client_to_update.reset = 0

            if telegram_id and (not hasattr(client_to_update, 'tg_id') or not client_to_update.tg_id):
                 client_to_update.tg_id = telegram_id

            client_uuid = client_to_update.id
            api.inbound.update(inbound_id, inbound_to_modify)
            logger.info(f"Updated existing client '{email}' (UUID: {client_uuid}) on inbound {inbound_id}")

        else:
            client_uuid = str(uuid.uuid4())
            subscription_id = uuid.uuid4().hex[:16]

            new_client = Client(
                id=client_uuid,
                email=email,
                enable=True,
                flow="xtls-rprx-vision",
                expiry_time=new_expiry_ms,
                sub_id=subscription_id,
                total_gb=0,
                reset=0,
                tg_id=telegram_id
            )
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

async def create_or_update_key_on_host(host_name: str, email: str, days_to_add: int, telegram_id: str = None) -> Dict | None:
    return await asyncio.to_thread(_create_or_update_key_on_host_sync, host_name, email, days_to_add, None, telegram_id)

async def create_or_update_key_on_host_seconds(host_name: str, email: str, seconds_to_add: int, telegram_id: str = None) -> Dict | None:
    return await asyncio.to_thread(_create_or_update_key_on_host_sync, host_name, email, 0, int(seconds_to_add), telegram_id)

def _create_or_update_key_on_host_sync(host_name: str, email: str, days_to_add: int, seconds_to_add: int | None, telegram_id: str = None) -> Dict | None:
    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Workflow failed: Host '{host_name}' not found in the database.")
        return None

    api, inbound = login_to_host(
        host_url=host_data['host_url'],
        username=host_data['host_username'],
        password=host_data['host_pass'],
        inbound_id=host_data['host_inbound_id']
    )
    if not api or not inbound:
        logger.error(f"Workflow failed: Could not log in or find inbound on host '{host_name}'.")
        return None
        
    client_uuid, new_expiry_ms = update_or_create_client_on_panel(api, inbound.id, email, days_to_add=days_to_add, seconds_to_add=seconds_to_add, telegram_id=telegram_id)
    if not client_uuid:
        logger.error(f"Workflow failed: Could not create/update client '{email}' on host '{host_name}'.")
        return None
    
    # Clean remark for URL safety
    safe_remark = host_name.replace(' ', '_').encode('ascii', 'ignore').decode('ascii')
    # Determine country flag based on server name
    country_flag = "🇺🇸"  # Default to USA
    host_lower = host_name.lower()

    # North America
    if "usa" in host_lower or "🇺🇸" in host_name or "united states" in host_lower or "america" in host_lower:
        country_flag = "🇺🇸"
    elif "canada" in host_lower or "🇨🇦" in host_name:
        country_flag = "🇨🇦"
    elif "mexico" in host_lower or "🇲🇽" in host_name:
        country_flag = "🇲🇽"

    # Europe
    elif "germany" in host_lower or "deutschland" in host_lower or "🇩🇪" in host_name:
        country_flag = "🇩🇪"
    elif "netherlands" in host_lower or "nederland" in host_lower or "🇳🇱" in host_name:
        country_flag = "🇳🇱"
    elif "france" in host_lower or "french" in host_lower or "🇫🇷" in host_name:
        country_flag = "🇫🇷"
    elif "uk" in host_lower or "united kingdom" in host_lower or "britain" in host_lower or "england" in host_lower or "🇬🇧" in host_name:
        country_flag = "🇬🇧"
    elif "italy" in host_lower or "italia" in host_lower or "🇮🇹" in host_name:
        country_flag = "🇮🇹"
    elif "spain" in host_lower or "españa" in host_lower or "🇪🇸" in host_name:
        country_flag = "🇪🇸"
    elif "sweden" in host_lower or "sverige" in host_lower or "🇸🇪" in host_name:
        country_flag = "🇸🇪"
    elif "norway" in host_lower or "norge" in host_lower or "🇳🇴" in host_name:
        country_flag = "🇳🇴"
    elif "denmark" in host_lower or "danmark" in host_lower or "🇩🇰" in host_name:
        country_flag = "🇩🇰"
    elif "finland" in host_lower or "suomi" in host_lower or "🇫🇮" in host_name:
        country_flag = "🇫🇮"
    elif "switzerland" in host_lower or "schweiz" in host_lower or "🇨🇭" in host_name:
        country_flag = "🇨🇭"
    elif "austria" in host_lower or "österreich" in host_lower or "🇦🇹" in host_name:
        country_flag = "🇦🇹"
    elif "poland" in host_lower or "polska" in host_lower or "🇵🇱" in host_name:
        country_flag = "🇵🇱"
    elif "czech" in host_lower or "česká" in host_lower or "🇨🇿" in host_name:
        country_flag = "🇨🇿"
    elif "hungary" in host_lower or "magyarország" in host_lower or "🇭🇺" in host_name:
        country_flag = "🇭🇺"
    elif "romania" in host_lower or "românia" in host_lower or "🇷🇴" in host_name:
        country_flag = "🇷🇴"
    elif "bulgaria" in host_lower or "българия" in host_lower or "🇧🇬" in host_name:
        country_flag = "🇧🇬"
    elif "greece" in host_lower or "ελλάδα" in host_lower or "🇬🇷" in host_name:
        country_flag = "🇬🇷"
    elif "turkey" in host_lower or "türkiye" in host_lower or "🇹🇷" in host_name:
        country_flag = "🇹🇷"
    elif "portugal" in host_lower or "🇵🇹" in host_name:
        country_flag = "🇵🇹"

    # Asia
    elif "japan" in host_lower or "nihon" in host_lower or "🇯🇵" in host_name:
        country_flag = "🇯🇵"
    elif "singapore" in host_lower or "🇸🇬" in host_name:
        country_flag = "🇸🇬"
    elif "south korea" in host_lower or "korea" in host_lower or "🇰🇷" in host_name:
        country_flag = "🇰🇷"
    elif "taiwan" in host_lower or "中華民國" in host_lower or "🇹🇼" in host_name:
        country_flag = "🇹🇼"
    elif "hong kong" in host_lower or "🇭🇰" in host_name:
        country_flag = "🇭🇰"
    elif "india" in host_lower or "भारत" in host_lower or "🇮🇳" in host_name:
        country_flag = "🇮🇳"
    elif "uae" in host_lower or "emirates" in host_lower or "🇦🇪" in host_name:
        country_flag = "🇦🇪"

    # Oceania
    elif "australia" in host_lower or "🇦🇺" in host_name:
        country_flag = "🇦🇺"

    # South America
    elif "brazil" in host_lower or "brasil" in host_lower or "🇧🇷" in host_name:
        country_flag = "🇧🇷"

    # Default remains 🇺🇸

    # Use server name (cleaned) with country flag for better UX
    # Clean server name: remove non-ASCII, replace spaces, keep only alphanumeric and underscores
    clean_server_name = host_name.replace(' ', '').encode('ascii', 'ignore').decode('ascii')
    # Remove any remaining special chars, keep only letters, numbers, underscores
    clean_server_name = ''.join(c for c in clean_server_name if c.isalnum() or c == '_')
    # Remove leading underscores
    clean_server_name = clean_server_name.lstrip('_')
    server_remark = f"{country_flag}{clean_server_name}"
    connection_string = get_connection_string(inbound, client_uuid, host_data['host_url'], remark=server_remark)
    
    logger.info(f"Successfully processed key for '{email}' on host '{host_name}'.")
    
    return {
        "client_uuid": client_uuid,
        "email": email,
        "expiry_timestamp_ms": new_expiry_ms,
        "connection_string": connection_string,
        "host_name": host_name
    }

async def get_key_details_from_host(key_data: dict) -> dict | None:
    return await asyncio.to_thread(_get_key_details_from_host_sync, key_data)

def _get_key_details_from_host_sync(key_data: dict) -> dict | None:
    host_name = key_data.get('host_name')
    if not host_name:
        logger.error(f"Could not get key details: host_name is missing for key_id {key_data.get('key_id')}")
        return None

    host_db_data = get_host(host_name)
    if not host_db_data:
        logger.error(f"Could not get key details: Host '{host_name}' not found in the database.")
        return None

    api, inbound = login_to_host(
        host_url=host_db_data['host_url'],
        username=host_db_data['host_username'],
        password=host_db_data['host_pass'],
        inbound_id=host_db_data['host_inbound_id']
    )
    if not api or not inbound: return None

    # Determine country flag based on server name
    country_flag = "🇺🇸"  # Default to USA
    host_lower = host_name.lower()

    # North America
    if "usa" in host_lower or "🇺🇸" in host_name or "united states" in host_lower or "america" in host_lower:
        country_flag = "🇺🇸"
    elif "canada" in host_lower or "🇨🇦" in host_name:
        country_flag = "🇨🇦"
    elif "mexico" in host_lower or "🇲🇽" in host_name:
        country_flag = "🇲🇽"

    # Europe
    elif "germany" in host_lower or "deutschland" in host_lower or "🇩🇪" in host_name:
        country_flag = "🇩🇪"
    elif "netherlands" in host_lower or "nederland" in host_lower or "🇳🇱" in host_name:
        country_flag = "🇳🇱"
    elif "france" in host_lower or "french" in host_lower or "🇫🇷" in host_name:
        country_flag = "🇫🇷"
    elif "uk" in host_lower or "united kingdom" in host_lower or "britain" in host_lower or "england" in host_lower or "🇬🇧" in host_name:
        country_flag = "🇬🇧"
    elif "italy" in host_lower or "italia" in host_lower or "🇮🇹" in host_name:
        country_flag = "🇮🇹"
    elif "spain" in host_lower or "españa" in host_lower or "🇪🇸" in host_name:
        country_flag = "🇪🇸"
    elif "sweden" in host_lower or "sverige" in host_lower or "🇸🇪" in host_name:
        country_flag = "🇸🇪"
    elif "norway" in host_lower or "norge" in host_lower or "🇳🇴" in host_name:
        country_flag = "🇳🇴"
    elif "denmark" in host_lower or "danmark" in host_lower or "🇩🇰" in host_name:
        country_flag = "🇩🇰"
    elif "finland" in host_lower or "suomi" in host_lower or "🇫🇮" in host_name:
        country_flag = "🇫🇮"
    elif "switzerland" in host_lower or "schweiz" in host_lower or "🇨🇭" in host_name:
        country_flag = "🇨🇭"
    elif "austria" in host_lower or "österreich" in host_lower or "🇦🇹" in host_name:
        country_flag = "🇦🇹"
    elif "poland" in host_lower or "polska" in host_lower or "🇵🇱" in host_name:
        country_flag = "🇵🇱"
    elif "czech" in host_lower or "česká" in host_lower or "🇨🇿" in host_name:
        country_flag = "🇨🇿"
    elif "hungary" in host_lower or "magyarország" in host_lower or "🇭🇺" in host_name:
        country_flag = "🇭🇺"
    elif "romania" in host_lower or "românia" in host_lower or "🇷🇴" in host_name:
        country_flag = "🇷🇴"
    elif "bulgaria" in host_lower or "българия" in host_lower or "🇧🇬" in host_name:
        country_flag = "🇧🇬"
    elif "greece" in host_lower or "ελλάδα" in host_lower or "🇬🇷" in host_name:
        country_flag = "🇬🇷"
    elif "turkey" in host_lower or "türkiye" in host_lower or "🇹🇷" in host_name:
        country_flag = "🇹🇷"
    elif "portugal" in host_lower or "🇵🇹" in host_name:
        country_flag = "🇵🇹"

    # Asia
    elif "japan" in host_lower or "nihon" in host_lower or "🇯🇵" in host_name:
        country_flag = "🇯🇵"
    elif "singapore" in host_lower or "🇸🇬" in host_name:
        country_flag = "🇸🇬"
    elif "south korea" in host_lower or "korea" in host_lower or "🇰🇷" in host_name:
        country_flag = "🇰🇷"
    elif "taiwan" in host_lower or "中華民國" in host_lower or "🇹🇼" in host_name:
        country_flag = "🇹🇼"
    elif "hong kong" in host_lower or "🇭🇰" in host_name:
        country_flag = "🇭🇰"
    elif "india" in host_lower or "भारत" in host_lower or "🇮🇳" in host_name:
        country_flag = "🇮🇳"
    elif "uae" in host_lower or "emirates" in host_lower or "🇦🇪" in host_name:
        country_flag = "🇦🇪"

    # Oceania
    elif "australia" in host_lower or "🇦🇺" in host_name:
        country_flag = "🇦🇺"

    # South America
    elif "brazil" in host_lower or "brasil" in host_lower or "🇧🇷" in host_name:
        country_flag = "🇧🇷"

    # Default remains 🇺🇸

    # Use server name (cleaned) with country flag for better UX
    # Clean server name: remove non-ASCII, replace spaces, keep only alphanumeric and underscores
    clean_server_name = host_name.replace(' ', '').encode('ascii', 'ignore').decode('ascii')
    # Remove any remaining special chars, keep only letters, numbers, underscores
    clean_server_name = ''.join(c for c in clean_server_name if c.isalnum() or c == '_')
    # Remove leading underscores
    clean_server_name = clean_server_name.lstrip('_')
    server_remark = f"{country_flag}{clean_server_name}"
    connection_string = get_connection_string(inbound, key_data['xui_client_uuid'], host_db_data['host_url'], remark=server_remark)
    return {"connection_string": connection_string}

async def get_client_traffic(key_data: dict) -> dict | None:
    return await asyncio.to_thread(_get_client_traffic_sync, key_data)

def _get_client_traffic_sync(key_data: dict) -> dict | None:
    host_name = key_data.get('host_name')
    if not host_name: return None

    host_db_data = get_host(host_name)
    if not host_db_data: return None

    api, inbound = login_to_host(
        host_url=host_db_data['host_url'],
        username=host_db_data['host_username'],
        password=host_db_data['host_pass'],
        inbound_id=host_db_data['host_inbound_id']
    )
    if not api or not inbound or not inbound.settings.clients: return None

    target_uuid = key_data.get('xui_client_uuid')
    for client in inbound.settings.clients:
        if client.id == target_uuid:
            return {
                "up": client.up,
                "down": client.down,
                "total": client.total,
                "expiry_time": client.expiry_time
            }
    return None

async def fix_client_parameters_on_host(host_name: str, client_email: str) -> bool:
    """Fix flow and encryption parameters for existing client on host"""
    return await asyncio.to_thread(_fix_client_parameters_on_host_sync, host_name, client_email)

def _fix_client_parameters_on_host_sync(host_name: str, client_email: str) -> bool:
    """Sync version of fix_client_parameters_on_host"""
    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Cannot fix client: Host '{host_name}' not found.")
        return False

    api, inbound = login_to_host(
        host_url=host_data['host_url'],
        username=host_data['host_username'],
        password=host_data['host_pass'],
        inbound_id=host_data['host_inbound_id']
    )

    if not api or not inbound:
        logger.error(f"Cannot fix client: Login or inbound lookup failed for host '{host_name}'.")
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

        # Fix client parameters
        inbound_to_modify.settings.clients[client_index].flow = "xtls-rprx-vision"
        try:
            inbound_to_modify.settings.clients[client_index].encryption = "none"
        except (ValueError, AttributeError):
            pass  # Field might not exist in some library versions, skip it

        api.inbound.update(inbound.id, inbound_to_modify)

        logger.info(f"Successfully fixed parameters for client '{client_email}' on host '{host_name}'.")
        return True

    except Exception as e:
        logger.error(f"Failed to fix client '{client_email}' on host '{host_name}': {e}", exc_info=True)
        return False

async def delete_client_on_host(host_name: str, client_email: str) -> bool:
    return await asyncio.to_thread(_delete_client_on_host_sync, host_name, client_email)

def _delete_client_on_host_sync(host_name: str, client_email: str) -> bool:
    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Cannot delete client: Host '{host_name}' not found.")
        return False

    api, inbound = login_to_host(
        host_url=host_data['host_url'],
        username=host_data['host_username'],
        password=host_data['host_pass'],
        inbound_id=host_data['host_inbound_id']
    )

    if not api or not inbound:
        logger.error(f"Cannot delete client: Login or inbound lookup failed for host '{host_name}'.")
        return False
        
    try:
        client_to_delete = get_key_by_email(client_email)
        if not client_to_delete:
            logger.warning(
                f"Client '{client_email}' not found in local database for host '{host_name}' (already deleted or out of sync)."
            )
            return True

        api.client.delete(inbound.id, client_to_delete['xui_client_uuid'])
        logger.info(f"Successfully deleted client '{client_to_delete['xui_client_uuid']}' from host '{host_name}'.")
        return True
            
    except Exception as e:
        logger.error(f"Failed to delete client '{client_email}' from host '{host_name}': {e}", exc_info=True)
        return False