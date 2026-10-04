# Camera SD → VPS → Telegram: pipeline audit (2026-10-04)

## Intended path

1. Every 900 seconds, search closed SD recordings using SDK metadata; identify
   recordings by camera, channel and recording start time, not a reusable SD filename.
2. Skip known records before downloading. Stage one bounded `.part` file and
   rename it only after the SDK reports download completion.
3. Remux to MP4 using FFmpeg `-c copy -movflags +faststart`. No video/audio
   encoder, decode validation or routine ffprobe is used. Unsupported input stays
   in `needs_review`; do not silently transcode, drop audio or rename raw bytes.
4. Commit the independent managed MP4 before releasing the staged source.
5. Stream multipart upload to Local Bot API, store its confirmed message/file
   references, then clean the managed MP4 according to retention. Ambiguous
   uploads stay quarantined rather than being resent automatically.

`KEEP_CACHE=false` with `CACHE_RETENTION_HOURS=0` removes confirmed archive files
immediately; `1` retains them for one hour. Unconfirmed and review files are
retained. Bot API's internal files are separately owned by Telegram's server;
archive cleanup does not delete its session/database or claim to clear all of
its caches. Its multipart spool now uses a separate disk-backed `bot-api-spool`
volume with a 5 GB cooperative admission budget, not the 256 MiB `/tmp` RAM
filesystem. Terminal-error media now expires after 72 hours; the catalog stays.
See [cache retention](CACHE_RETENTION.md) for the current policy.

## Changes from the audit

- Bound each native request, rather than expiring a healthy helper after 30
  minutes including time spent remuxing/uploading other clips. Hung calls remain
  bounded; the helper is still isolated and terminated on timeout.
- Throttle repeated known/deferred/backlog progress to one update per second;
  retain forced search/end and new-file notifications for interleaved uploads.
- Aggregate counts in SQLite with a camera/status/deletion/retry covering index
  instead of fetching the complete camera catalog on every progress callback.
- Release the staged source after successful ingest, before waiting for upload.
- Reserve source plus worst-case output space *before* downloading; remux uses
  a conservative 3× input reservation, raw transfer 2×.
- Record `sd_connect_seconds`, `sd_search_seconds`, `sd_download_seconds`,
  `sd_ingest_seconds`, `sd_download_bytes`, `upload_seconds` in sync-job statistics.
  These are cumulative completed stages, not an estimate for a currently hung
  operation. Upload time includes API round trip and cleanup; ingest includes
  remux, file integrity checks and catalog commit.

## Measured before deployment

One already-archived 752,080-byte camera clip was downloaded and remuxed in an
isolated temporary folder, with zero Telegram posts and unchanged catalog:

| Stage | Seconds |
|---|---:|
| SDK connection | 7.598 |
| Search | 0.411 |
| SD download | 4.054 |
| Stream-copy remux (725,836-byte MP4) | 0.135 |

The sampled Tailscale peer was direct after transfer. This sample points to
camera connection/download latency, not expensive video encoding. It does not
establish a bandwidth ceiling for all cameras or routes. Recent catalog timing
from ingest creation to upload confirmation was 0.447–1.450 seconds; that is not
pure upload time. Prior 30-minute jobs ended in `sd_native_worker_timeout` despite
having made progress: the old absolute helper deadline explains that failure.

A deterministic synthetic 1,000-known-record scan changed progress callbacks
from **1,003 to 3** (99.7% fewer), with zero downloads in both cases. This is
reduced metadata/database overhead, not a 334× network throughput claim.
At upload callback entry, staged-source bytes changed from 8 to 0 in the tiny
fixture; the independent managed artifact remained present.

Two existing files had failed remux and remain retained for review. A bounded
metadata-only diagnostic did not identify a usable video codec. This audit does
not turn those files into valid MP4s by renaming or transcoding them.

## Operational choices

Keep per-camera sequential download/remux/upload and the 15-minute schedule for
now. Avoid speculative parallel SDK sessions or undocumented speed controls;
measure the new phase metrics first. Preserve retries, upload-uncertainty guards,
closed-record settle time and database identity. An MP4 container alone does not
guarantee that every client supports the camera's original codec.

## Primary references

- [FFmpeg streamcopy](https://ffmpeg.org/ffmpeg.html#Streamcopy)
- [Telegram Local Bot API](https://core.telegram.org/bots/api#using-a-local-bot-api-server)
- [Telegram server temporary-directory handling](https://github.com/tdlib/telegram-bot-api/blob/e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1/telegram-bot-api/telegram-bot-api.cpp)
- [Hikvision download by native filename](https://open.hikvision.com/hardware/definitions/NET_DVR_GetFileByName.html)
