"""Non-blocking process ownership, shared by all containers using these paths."""
import fcntl
import os
from pathlib import Path


class InstanceAlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, descriptors):
        self._descriptors = descriptors

    def close(self):
        for descriptor in self._descriptors:
            os.close(descriptor)
        self._descriptors.clear()


def acquire_instance_lock(database, project_root):
    database = Path(database).resolve()
    root = Path(project_root).resolve()
    paths = {root / '.shop_bot.instance.lock',
             database.with_name('.' + database.name + '.instance.lock')}
    descriptors = []
    try:
        for path in sorted(paths):
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            descriptors.append(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InstanceAlreadyRunning('Another application instance is already running') from None
        return InstanceLock(descriptors)
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise
