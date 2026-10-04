"""Metadata-only mapping of a managed MP4 into the Local Bot API cache mount."""
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import quote


class LocalUploadError(ValueError):
    """A known-unsent local-path error; do not retry through another transport."""


def validate_upload_root(value):
    # This is a Linux container path even when fixtures run on Windows.
    if (not isinstance(value, str) or not value.startswith('/') or value.startswith('//')
            or value == '/' or '\\' in value or '\x00' in value
            or any(part in ('.', '..') for part in value.split('/'))
            or any(ord(c) < 32 for c in value)):
        raise LocalUploadError('Invalid local upload mount')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise LocalUploadError('Invalid local upload mount') from None
    return PurePosixPath(value)


def local_mp4_uri(settings, file_path, *, expected_key=None, cache_root=None):
    """Check the cache identity without hashing, probing, or reading video bytes."""
    if settings.api_mode != 'local' or settings.upload_transport != 'local_file':
        raise LocalUploadError('Local upload transport is not configured')
    target = validate_upload_root(settings.local_upload_root)
    path = Path(file_path).absolute()
    key = path.stem if expected_key is None else expected_key
    if not isinstance(key, str) or re.fullmatch(r'[a-f0-9]{64}', key) is None:
        raise LocalUploadError('Invalid managed MP4 identity')
    root = settings.cache_dir.absolute()
    try:
        if (root.resolve(strict=True) != root or not stat.S_ISDIR(root.lstat().st_mode)
                or cache_root is not None and root != cache_root
                or path != root / (key + '.mp4')
                or not stat.S_ISREG(path.lstat().st_mode)
                or path.resolve(strict=True) != path
                or path.stat().st_size <= 0):
            raise LocalUploadError('Invalid managed MP4 path')
    except (OSError, RuntimeError, ValueError):
        raise LocalUploadError('Invalid managed MP4 path') from None
    return 'file://' + quote(str(target / path.name), safe='/')
