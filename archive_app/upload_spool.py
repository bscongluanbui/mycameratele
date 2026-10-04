"""Cooperative admission for Local Bot API's separate disk-backed HTTP spool.

This is an application budget, not a filesystem quota or a deletion policy.
The Bot API owns live multipart files and removes them when requests finish.
"""
import os
from pathlib import Path
import shutil
import stat


class SpoolBudgetError(ValueError):
    """The next upload must wait without sending any multipart bytes."""


def _used_bytes(root, ceiling):
    """Count regular files without following links; tolerate concurrent unlink."""
    visited = 0
    used = 0
    # POSIX directory descriptors keep a rename/link race inside the opened
    # spool. Windows uses checked paths for the same portable fixture contract.
    descriptor_mode = os.name != 'nt' and hasattr(os, 'O_NOFOLLOW')
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)

    def visit(location, depth=0):
        nonlocal visited, used
        if depth > 64:
            raise SpoolBudgetError('upload_spool_budget')
        with os.scandir(location) as entries:
            for entry in entries:
                visited += 1
                if visited > 100000:
                    raise SpoolBudgetError('upload_spool_budget')
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # Bot API has just completed this request.
                if stat.S_ISREG(info.st_mode):
                    used += info.st_size
                    if used > ceiling:
                        raise SpoolBudgetError('upload_spool_budget')
                elif stat.S_ISDIR(info.st_mode):
                    if descriptor_mode:
                        try:
                            child = os.open(entry.name, flags, dir_fd=location)
                        except FileNotFoundError:
                            continue
                        try:
                            visit(child, depth + 1)
                        finally:
                            os.close(child)
                    else:
                        child = Path(entry.path)
                        try:
                            if child.resolve(strict=True) != child.absolute():
                                raise SpoolBudgetError('upload_spool_budget')
                            visit(child, depth + 1)
                        except FileNotFoundError:
                            continue
                else:
                    # No symlink, FIFO, device or socket belongs in this spool.
                    raise SpoolBudgetError('upload_spool_budget')

    if descriptor_mode:
        descriptor = os.open(root, flags)
        try:
            visit(descriptor)
            disk = os.fstatvfs(descriptor)
            free = disk.f_bavail * disk.f_frsize
        finally:
            os.close(descriptor)
    else:
        visit(root)
        if root.resolve(strict=True) != root.absolute():
            raise SpoolBudgetError('upload_spool_budget')
        free = shutil.disk_usage(root).free
    return used, free


def check_upload_spool(settings, incoming_bytes):
    """Admit a bounded upload or raise before HTTP POST; no file is modified.

    Legacy direct constructors with no spool root retain their prior behavior.
    Production local Compose always supplies the dedicated mounted directory.
    """
    root = getattr(settings, 'bot_api_spool_root', None)
    if settings.api_mode != 'local' or root is None:
        return None
    budget = getattr(settings, 'bot_api_spool_max_bytes', 5000000000)
    reserve = settings.min_free_bytes
    if (type(incoming_bytes) is not int or incoming_bytes <= 0 or
            type(budget) is not int or budget <= 0 or
            type(reserve) is not int or reserve < 0 or incoming_bytes > budget):
        raise SpoolBudgetError('upload_spool_budget')
    root = Path(root).absolute()
    try:
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or root.resolve(strict=True) != root:
            raise SpoolBudgetError('upload_spool_budget')
        used, free = _used_bytes(root, budget - incoming_bytes)
    except (OSError, RuntimeError, ValueError):
        raise SpoolBudgetError('upload_spool_budget') from None
    if used + incoming_bytes > budget or free < incoming_bytes + reserve:
        raise SpoolBudgetError('upload_spool_budget')
    return {'used_bytes': used, 'incoming_bytes': incoming_bytes,
            'budget_bytes': budget, 'free_bytes': free}
