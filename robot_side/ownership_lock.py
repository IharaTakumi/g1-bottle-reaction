"""Cooperative Linux writer exclusion. Never unlink or replace the lock inode."""
import os
import stat

CANONICAL_LOCK_PATH = "/tmp/g1-project-locomotion.lock"
_held_descriptors = []  # Intentionally retained until process exit, even on error.


def acquire_process_lock(path=CANONICAL_LOCK_PATH):
    """The path argument is for offline tests, not a production CLI override."""
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o660)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("locomotion lock must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    _held_descriptors.append(fd)
    return fd
