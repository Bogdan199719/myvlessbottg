# Re-export from database.py for backwards compatibility.
# The canonical implementations with proper error handling live in database.py.
from shop_bot.data_manager.database import get_user_paid_keys, get_user_trial_keys

__all__ = ["get_user_paid_keys", "get_user_trial_keys"]
