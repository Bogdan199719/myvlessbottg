"""Bounded failure tracking. Call under the login handler's existing lock."""

MAX_TRACKED_IPS = 4096


def prune_login_attempts(attempts, now, window_seconds):
    for ip, timestamps in list(attempts.items()):
        recent = [stamp for stamp in timestamps if now - stamp < window_seconds]
        if recent:
            attempts[ip] = recent
        else:
            del attempts[ip]


def login_is_limited(attempts, ip, now, window_seconds, max_attempts):
    prune_login_attempts(attempts, now, window_seconds)
    if len(attempts.get(ip, ())) >= max_attempts:
        return True
    # Keep active bans intact. Fail closed for new sources when capacity is full.
    return ip not in attempts and len(attempts) >= MAX_TRACKED_IPS


def record_login_failure(attempts, ip, now, window_seconds, max_attempts):
    prune_login_attempts(attempts, now, window_seconds)
    if ip not in attempts and len(attempts) >= MAX_TRACKED_IPS:
        return
    # The latest N timestamps preserve the >=N-in-window predicate even when
    # concurrent password checks complete out of order. Never discard newer failures.
    attempts[ip] = sorted([*attempts.get(ip, ()), now])[-max_attempts:]
