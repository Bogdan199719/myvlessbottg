---
description: How to deploy and update the VLESS Shop Bot application
---

# Deployment & Update Workflow

This workflow describes how to update the application on the server while preserving critical data (`users.db`).  The user updates files locally and then syncs them to the server, but `users.db` must be handled with care.

## 1. Stop Existing Containers
On the server, stop the running containers to ensure files are not locked.
```bash
docker-compose down
```

## 2. Preserve Database (Crucial Step)
**IMPORTANT**: The `users.db` file contains all user data. It is located in the root directory.
Verify `users.db` exists and, if desired, create a backup:
```bash
cp users.db users.db.backup_$(date +%Y%m%d)
```
*Note: Since the user replaces the `src` folder and other files, `users.db` usually stays in the root if not explicitly overwritten. However, if the user replaces the **entire** folder, they must ensure `users.db` is copied back or preserved.*

## 3. Update Codebase
Replace the project files on the server with the new version from your local machine.
**CRITICAL**: Do NOT overwrite `users.db` with a fresh/empty one if you are just updating code.

## 4. Rebuild and Start
Rebuild the Docker image to include any new dependencies or code changes, then start in detached mode.
```bash
docker-compose up -d --build
```

## 5. Verification
Check the logs to ensure the bot started correctly and connected to the existing database.
```bash
docker-compose logs -f --tail=100
```
Look for: `Database initialized successfully at .../users.db`
