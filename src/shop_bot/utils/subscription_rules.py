"""Shared classification of trial and extended free VPN access."""

import math

from shop_bot.utils import time_utils


def configured_trial_days(value: str | float | None) -> float:
    try:
        days = float(value or 1)
    except (TypeError, ValueError):
        return 1.0
    return max(1.0, days) if math.isfinite(days) else 1.0


def is_trial_xui_key(key: dict, trial_duration_days: float = 1.0) -> bool:
    """Match the admin's trial rule, including its six-hour legacy tolerance.

    A zero plan ID survives manual extensions. It alone cannot distinguish a
    one-day trial from a longer free grant. Missing legacy dates retain the
    previous conservative trial classification.
    """
    if key.get("service_type", "xui") == "mtg":
        return False
    try:
        plan_id = int(key.get("plan_id") or 0)
    except (TypeError, ValueError):
        plan_id = 0
    if plan_id > 0:
        return False

    created = time_utils.parse_iso_to_msk(key.get("created_date"))
    expiry = time_utils.parse_iso_to_msk(key.get("expiry_date"))
    if not created or not expiry:
        return True
    return (expiry - created).total_seconds() <= (
        configured_trial_days(trial_duration_days) * 86400 + 6 * 3600
    )
