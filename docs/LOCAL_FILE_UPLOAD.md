# Local Bot API reads the managed MP4 directly

## Flow

```text
Camera SD → managed VPS cache → MP4 stream-copy remux
          → Local Bot API reads file:///cache/...mp4 → Telegram channel
          → catalog commits confirmed Message/file references → cleanup
```

The worker sends the managed MP4's file URI and upload metadata rather than
streaming its media bytes in an HTTP multipart body. The Bot API mounts the
existing `archive-cache` volume at `/cache` **read-only**, at the same path as
the worker. This removes the worker-to-API media transfer and corresponding
HTTP multipart temporary copy for that MP4. It does not transcode the media or
change the Internet upload to Telegram. Bot API/TDLib can still read/copy/cache
the file internally; this is not an end-to-end zero-copy or no-cache guarantee.
There is no claimed speedup until an actual before/after measurement is made.

Only eligible managed MP4 `sendVideo` uploads use this route. Cloud API,
raw/document uploads and explicitly selected `multipart` retain the existing
streamed-multipart route. Bot commands, channel references and album delivery
continue to use Telegram metadata/file IDs as before.

## Compose settings

The base `compose.yaml` defaults to `TELEGRAM_UPLOAD_TRANSPORT=multipart` and
an empty `TELEGRAM_LOCAL_UPLOAD_ROOT`. It does not mount the camera cache in the
Bot API service. The production Local Bot API override selects:

```dotenv
TELEGRAM_API_MODE=local
TELEGRAM_UPLOAD_TRANSPORT=local_file
TELEGRAM_LOCAL_UPLOAD_ROOT=/cache
```

Leave `TELEGRAM_UPLOAD_TRANSPORT=` blank in `.env` to use the defaults chosen by
the selected Compose files. `compose.local.yaml` maps the API root to `/cache`;
the uploaded file's path relative to the worker cache is preserved. A direct
host/custom deployment must supply the matching API-visible cache root and
make that same managed cache accessible to the Bot API account.

The named volumes keep their existing identities: `archive-state`,
`archive-cache`, `bot-api-state`, `bot-api-spool`. The Local Bot API service
retains its persistent state and disk-backed temporary spool. It gains only
the read-only archive cache mount. No cache data migration or new media volume
is required.

For a project that has not persisted its Compose selection:

```sh
docker compose -f compose.yaml -f compose.local.yaml --profile local-api pull
docker compose -f compose.yaml -f compose.local.yaml --profile local-api up -d
```

Keep any already-configured SDK override and other project overrides in the
command or existing `COMPOSE_FILE`. An established deployment can continue
using `docker compose pull` and `docker compose up -d` with that selection.

## Retention and capacity

The worker retains the MP4 throughout the upload request, including the
existing **1800-second** media-upload timeout. Telegram's confirmed response
must be saved in the catalog before immediate cleanup deletes the managed
MP4 and its PS/TS/staging siblings. Ambiguous uploads remain quarantined; no
success is inferred from submitting a file URI. The existing terminal-error
72-hour expiry policy remains unchanged.

The **5,000,000,000-byte** spool admission budget stays configured. Direct MP4
uploads do not reserve a new incoming multipart file's size there, but still
check existing spool usage and the disk free-space reserve. Raw/multipart
uploads continue to reserve their incoming temporary copy. The budget is a
cooperative admission check, not a filesystem quota. The worker does not
delete the Bot API's live temporary files or internal session/cache files.

## Opt out without changing data

Set the following in the deployment's `.env` and recreate the application
services using the same Compose file selection:

```dotenv
TELEGRAM_UPLOAD_TRANSPORT=multipart
```

The shared cache can remain mounted read-only; multipart does not read it
through the Bot API. This changes only the transport for subsequent requests,
not the recording catalog, channel posts, media format or retention settings.
Drain an active upload before an operational transport change.

## Primary reference

Telegram documents local-path and file-URI uploads in its
[Local Bot API server documentation](https://core.telegram.org/bots/api#using-a-local-bot-api-server).
