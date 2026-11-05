import logging
import os
import re
import subprocess
import time
from pathlib import Path

import requests

from shop_bot.version import APP_VERSION, REPO_OWNER, REPO_NAME

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAFE_GIT_DIRECTORY = "/app/project"
_VERSION_PATTERN = re.compile(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']')
_TRUTHY_VALUES = {"1", "true", "yes", "on"}


def _run_command(
    command: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd) if cwd else None,
        timeout=30,
    )


def _normalize_version_parts(version: str) -> tuple[int | str, ...]:
    parts: list[int | str] = []
    for part in re.split(r"[.\-_]+", version.lstrip("v")):
        if not part:
            continue
        parts.append(int(part) if part.isdigit() else part.lower())
    return tuple(parts)


def _is_remote_version_newer(remote_version: str, current_version: str) -> bool:
    remote_parts = _normalize_version_parts(remote_version)
    current_parts = _normalize_version_parts(current_version)

    max_len = max(len(remote_parts), len(current_parts))
    for index in range(max_len):
        remote_part = remote_parts[index] if index < len(remote_parts) else 0
        current_part = current_parts[index] if index < len(current_parts) else 0
        if remote_part == current_part:
            continue
        if isinstance(remote_part, int) and isinstance(current_part, int):
            return remote_part > current_part
        return str(remote_part) > str(current_part)

    return False


def _worktree_has_local_changes() -> tuple[bool, str]:
    try:
        result = _run_command(["git", "status", "--porcelain"], cwd=_REPO_ROOT)
    except Exception as e:
        return True, f"Не удалось проверить git status: {e}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return True, stderr or "git status завершился с ошибкой"

    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        return False, ""

    preview = ", ".join(line[3:] if len(line) > 3 else line for line in lines[:5])
    if len(lines) > 5:
        preview += ", ..."
    return True, preview


def check_for_updates() -> dict:
    """
    Checks for updates via GitHub RAW content from main branch.
    Returns: {"update_available": bool, "latest_version": str, "current_version": str, "release_notes": str}
    """
    try:
        url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/src/shop_bot/version.py?t={int(time.time())}"
        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            content = response.text
            match = _VERSION_PATTERN.search(content)
            if not match:
                return {"error": "Could not parse version from remote version.py"}

            latest_version = match.group(1).lstrip("v")
            current = APP_VERSION.lstrip("v")

            return {
                "update_available": _is_remote_version_newer(latest_version, current),
                "latest_version": latest_version,
                "current_version": current,
                "release_notes": "Обновление функционала и исправление ошибок.",
            }

        logger.warning("Failed to check for updates: %s", response.status_code)
        return {"error": f"GitHub Check Failed: {response.status_code}"}
    except Exception as e:
        logger.error("Error checking updates: %s", e)
        return {"error": str(e)}


def perform_update() -> dict:
    """Fail closed: releases require a verified image and a data-preserving rollback."""
    if os.getenv("ENABLE_WEB_UPDATES", "").strip().lower() not in _TRUTHY_VALUES:
        return {
            "status": "error",
            "message": "Web-обновления отключены в production. Используйте проверенный образ приложения.",
        }
    return {
        "status": "error",
        "message": (
            "Изменение работающих исходников через git/pip запрещено. "
            "Обновите проверенный образ после резервного копирования; "
            "откат должен сохранять текущую базу."
        ),
    }
