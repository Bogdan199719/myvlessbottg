import logging
import os
import subprocess
import sys
import requests
from shop_bot.version import APP_VERSION, REPO_OWNER, REPO_NAME

logger = logging.getLogger(__name__)

def check_for_updates() -> dict:
    """
    Checks for updates via GitHub RAW content from main branch.
    Returns: {"update_available": bool, "latest_version": str, "current_version": str, "release_notes": str}
    """
    try:
        # We fetch the raw version.py from the main branch
        # Added timestamp to bypass GitHub/proxies cache
        import time
        url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/src/shop_bot/version.py?t={int(time.time())}"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            content = response.text
            # Simple parsing for APP_VERSION = "..."
            import re
            match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', content)
            if not match:
                return {"error": "Could not parse version from remote version.py"}
            
            latest_version = match.group(1).lstrip("v")
            current = APP_VERSION.lstrip("v")
            
            update_available = latest_version != current
            
            return {
                "update_available": update_available,
                "latest_version": latest_version,
                "current_version": current,
                "release_notes": "Обновление функционала и исправление ошибок."
            }
        else:
            logger.warning(f"Failed to check for updates: {response.status_code}")
            return {"error": f"GitHub Check Failed: {response.status_code}"}
    except Exception as e:
        logger.error(f"Error checking updates: {e}")
        return {"error": str(e)}

def perform_update() -> dict:
    """
    Performs `git fetch`, `git reset --hard`, `pip install`, and restarts the application.
    This method ensures that any local modifications on the server are overwritten
    by the latest version from GitHub.
    """
    try:
        # 1. Configure git safe directory to avoid ownership errors in Docker
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", "/app/project"], check=False)

        # 2. Fetch latest changes
        logger.info("Fetching latest changes from GitHub...")
        subprocess.run(["git", "fetch", "origin", "main"], check=False)
        
        # 3. Reset --hard to origin/main (to overwrite local conflicts)
        logger.info("Resetting local state to match GitHub (Force Update)...")
        result = subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, text=True, check=False)
        
        if result.returncode != 0:
            return {"status": "error", "message": f"Git Reset Failed: {result.stderr}"}
        
        # 4. Pip Install in editable mode to accept new dependencies and keep volume sync
        logger.info("Updating dependencies...")
        install_result = subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."], capture_output=True, text=True, check=False)

        if install_result.returncode != 0:
            logger.error(f"Dependency update failed: {install_result.stderr}")
            # We continue despite dependency error, as restart might still work if deps are fine

        
        logger.info("Update successful. Triggering restart in 3 seconds...")
        
        # We use a thread to exit after a delay, allowing the Flask response to be sent
        import threading
        import time
        def delayed_restart():
            time.sleep(3)
            logger.info("Restarting process now...")
            os._exit(0) # Forced exit to trigger Docker restart
            
        threading.Thread(target=delayed_restart).start()

        return {"status": "success", "message": "Обновление скачано. Бот перезагрузится через несколько секунд..."}


    except Exception as e:
        logger.error(f"Update failed: {e}")
        return {"status": "error", "message": str(e)}
