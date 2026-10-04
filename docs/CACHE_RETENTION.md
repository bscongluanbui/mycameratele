# VPS media retention and 5 GB upload spool

Production defaults:

```dotenv
KEEP_CACHE=false
CACHE_RETENTION_HOURS=0
ERROR_RETENTION_HOURS=72
BOT_API_SPOOL_MAX_GB=5
```

## Successful upload

After Telegram confirms the Message and the catalog commits its channel/file
references, delete that recording's managed MP4, PS, TS, BIN and partial/staging
aliases on the VPS. Cleanup uses the stable recording key, not a cache-wide
extension glob. Original Studio input files, camera SD recordings, unrelated
operator files and Telegram channel posts are not deleted. A migration revision
sweeps older already-cleaned catalog rows once without scanning them on every
worker tick. Telegram references remain usable for playback and album delivery.

## Terminal error files: 72 hours

The first transition to `failed`, `needs_review` or `upload_unknown` starts a
72-hour clock. Repeated scans/retries/restarts do not reset it. At the first
worker maintenance pass after the deadline, delete the recording's managed
media and retain an expiry tombstone, original status/error and catalog IDs.
SD and manifest scans do not recreate expired footage automatically. No active
`uploading`/`ingesting` file or downloaded file waiting in 429/capacity backoff
enters this sweep. Expiry applies even if `KEEP_CACHE=true` is set for confirmed
uploads. A previously ambiguous upload can still be reconciled using confirmed
Telegram metadata after its local media expires.

Old errors have no trustworthy first-failure clock, so their 72-hour window
begins once at the upgrade. There is no fabricated earlier failure timestamp.
Expiry runs on exclusive-worker startup and subsequent maintenance loops;
long-running camera operations can delay a pass. Partial failed downloads are
discarded by the adapter; complete retained error copies follow this policy.

## Local Bot API spool

`bot-api-spool` is a separate **disk-backed Docker named volume**. The Bot API
writes HTTP multipart temporary files there; the worker mounts it read-only to
check capacity. Persistent Telegram session/database files stay in the existing
`bot-api-state` volume. A network-disabled one-shot initialization service owns
only the spool mount and gives UID10001 access.

The worker admits a new upload only when current spool bytes plus its file size
fit **5,000,000,000 bytes**, and the disk retains its configured free-space
reserve. Otherwise it sends no multipart POST, keeps the video queued, reports
`upload_spool_budget` and retries after 60 seconds. This is a cooperative
application admission budget, **not** a filesystem quota or a 5 GB RAM tmpfs.
The actual disk must have sufficient free space; the budget cannot manufacture
disk capacity. Sequential <=2 GB uploads fit the default budget. Other clients
writing directly to the Local Bot API are outside this admission check.

The Telegram server owns deletion of its live HTTP temporary files. Do not
recursively delete `bot-api-state` or sweep live API files from the worker.
The camera media expiry timer applies to archive-owned cache files.

Upgrades preserve the original three volumes and add only the spool volume.
Standard local Compose startup starts `spool-init` before the Bot API:

```sh
docker compose pull
docker compose up -d
```

For a newly configured Local Bot API project, include `compose.local.yaml` and
the `local-api` profile as documented in the installation guide. Existing
deployments retain their configured Compose file/profile settings.
