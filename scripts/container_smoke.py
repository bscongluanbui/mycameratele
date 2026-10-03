"""Real FFmpeg/SQLite smoke executed in each published Linux image by CI.

Input is generated color/silence, never camera footage. No network credentials.
Run with writable /input, /data, /cache tmpfs and a read-only root filesystem.
"""
import hashlib
import json
import os
import platform
import subprocess
from datetime import timedelta
from pathlib import Path

from archive_app.core import Archive, Settings, get_zone, parse_time
from archive_app.telegram import Telegram

assert os.getuid() == 10001, "Image must run as its non-root application user"
settings = Settings.from_env()
assert not settings.enable_upload
settings.min_free_bytes = 0  # Synthetic tmpfs smoke must not require 5 GB free RAM.
source = settings.input_dir / "synthetic-smoke.mp4"
subprocess.run([
    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
    "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=12",
    "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
    "-threads", "1", str(source),
], check=True, timeout=120)
original = hashlib.sha256(source.read_bytes()).hexdigest()
entry = {
    "camera": "smoke_camera", "source": "synthetic-ci", "record_id": "smoke-001",
    "path": source.name, "start_time": "2026-10-03T10:00:00+07:00",
    "end_time": "2026-10-03T10:00:01+07:00",
}
archive = Archive(settings)
try:
    row = archive.ingest_entry(entry)
    assert row["status"] == "downloaded"
    assert row["codec_video"] == "h264" and row["codec_audio"] == "aac"
    assert archive.ingest_entry(entry)["key"] == row["key"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert Path(row["local_path"]).is_file()
    assert Telegram(settings).upload_one(archive) is None
    assert not archive.list_day("2026-10-03", "smoke_camera")
    assert parse_time(entry["start_time"]).astimezone(get_zone(settings.timezone)).utcoffset() == timedelta(hours=7)
    reopened = Archive(settings)
    try:
        assert reopened.browse(camera="smoke_camera", status="all")["total"] == 1
    finally:
        reopened.close()
finally:
    archive.close()
print(json.dumps({"result": "OK", "machine": platform.machine(), "uid": os.getuid(),
                  "ffmpeg": "h264+aac-remux-decode", "sqlite": "durable-idempotent",
                  "source": "unchanged", "timezone": "+07:00", "telegram_posts": 0}, sort_keys=True))
