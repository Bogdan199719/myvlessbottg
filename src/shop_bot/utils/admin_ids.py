import re


def parse_admin_telegram_ids(value: object) -> tuple[int, ...]:
    """Parse the legacy single-ID setting or a comma/space separated ID list."""
    if value is None:
        return ()

    admin_ids: list[int] = []
    for item in re.split(r"[\s,;]+", str(value).strip()):
        if not item:
            continue
        try:
            admin_id = int(item)
        except ValueError:
            continue
        if admin_id > 0 and admin_id not in admin_ids:
            admin_ids.append(admin_id)
    return tuple(admin_ids)


def normalize_admin_telegram_ids(value: object) -> str:
    """Validate and normalize an administrator ID list for persistent settings."""
    raw_value = "" if value is None else str(value).strip()
    if not raw_value or not re.fullmatch(r"\d+(?:[\s,;]+\d+)*", raw_value):
        raise ValueError("Administrator IDs must be positive integers.")

    admin_ids = parse_admin_telegram_ids(raw_value)
    if not admin_ids:
        raise ValueError("At least one administrator ID is required.")
    return ",".join(str(admin_id) for admin_id in admin_ids)


def is_admin_telegram_id(user_id: object, configured_ids: object) -> bool:
    try:
        normalized_user_id = int(user_id)
    except (TypeError, ValueError):
        return False
    return normalized_user_id in parse_admin_telegram_ids(configured_ids)
