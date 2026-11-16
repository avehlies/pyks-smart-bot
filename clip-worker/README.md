# YKS clip worker

A sidecar to the YKS Smart Bot that transcribes the clips the bot records, so `/findtheclimp` can search them. It is a separate application, with its own process, Docker image and dependencies (faster-whisper). It imports no bot code; the two share only the database.

## How it works

1. The bot records each audio/video attachment posted in the clip channel as a `pending` row in `clips`. A database trigger sends `NOTIFY clips_new`.
2. The worker wakes up on that notification, or every `POLL_INTERVAL_SECONDS` as a fallback. It claims the newest pending clip with `SELECT ... FOR UPDATE SKIP LOCKED`, so several workers can run side by side.
3. It asks the Discord API for the message to get a **fresh** attachment URL, since stored CDN links expire. If the message is gone, the clip is marked `deleted` / `skipped`.
4. It downloads the file to a temp folder and transcribes it with faster-whisper, in a child process that's started on demand (see [Memory](#memory)). faster-whisper decodes audio and video itself through PyAV, so no ffmpeg is needed.
5. It stores the transcript (`done`), along with the model that made it (`transcribed_model`). Postgres keeps the full-text search column up to date automatically.
6. When no new clips are waiting, it re-transcribes old clips made with a different model (see [Changing the model](#changing-the-model)).

Failures are retried with a backoff of 1, 4, 9 … minutes, up to `MAX_ATTEMPTS`, then marked `failed`, with the error in `last_error`. A clip left in `processing` by a killed worker goes back to `pending` after `STALE_AFTER_MINUTES`. On a normal shutdown (SIGTERM), the current clip is handed back immediately.

## Memory

The Whisper model runs in a **child process** that's started when a clip arrives and stopped after `WHISPER_IDLE_UNLOAD_SECONDS` (default 5 minutes) with no clips. When it stops, the operating system reclaims all of the model's memory, so the worker sits at its idle baseline (about 40–60 MB) most of the time. The next clip starts a fresh child and reloads the model, which takes a few seconds for `tiny.en`. A burst of clips reuses the same child.

Deleting the model inside the long-running process wouldn't reliably return memory to the OS, because the allocator tends to keep freed memory for reuse. A separate process guarantees it. If the child crashes, that clip is retried later and the next clip starts a new child.

Set `WHISPER_IDLE_UNLOAD_SECONDS=0` to keep the model loaded all the time (faster first transcription, constant memory use).

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://yks:yks@localhost:5432/yks` | Same database as the bot. `postgresql+asyncpg://` URLs are accepted. |
| `AUTH_TOKEN` | (required) | The bot's token, used read-only to refresh attachment URLs. |
| `WHISPER_MODEL` | `tiny.en` | Any faster-whisper model (`base.en`, `small.en`, `large-v3`, …). Downloaded on first use into `HF_HOME` (`/models` in Docker). |
| `WHISPER_DEVICE` | `cpu` | `cuda` for a GPU. |
| `WHISPER_COMPUTE_TYPE` | `int8` | `float16` on a GPU. |
| `WHISPER_BEAM_SIZE` | `3` | Lower is faster on a CPU (`1` is greedy decoding), higher is slightly more accurate (`5` is faster-whisper's usual default). |
| `WHISPER_IDLE_UNLOAD_SECONDS` | `300` | Stop the model's process after this long with no clips. `0` keeps it loaded. |
| `RETRANSCRIBE` | `true` | When idle, redo clips transcribed with a different `WHISPER_MODEL`. |
| `MAX_ATTEMPTS` | `3` | Also limits attempts to re-transcribe a clip. |
| `POLL_INTERVAL_SECONDS` | `60` | |
| `STALE_AFTER_MINUTES` | `30` | |
| `MAX_DOWNLOAD_MB` | `500` | Bigger clips are marked `failed`. |

## Running

With Docker, it's the `clip-worker` service in the repository's `docker-compose.yml`.

Locally, from the repository root:

```sh
uv sync --all-packages
uv run yks-clip-worker
```

## Checking on it

```sql
SELECT status, count(*) FROM clips GROUP BY status;
SELECT id, attempts, last_error FROM clips WHERE status = 'failed' ORDER BY id DESC LIMIT 20;
-- retry failed clips:
UPDATE clips SET status = 'pending', attempts = 0, retry_at = NULL WHERE status = 'failed';
```

## Changing the model

Set `WHISPER_MODEL` and recreate the worker. New clips use the new model right away. Whenever no new clips are waiting, the worker also re-transcribes old clips whose `transcribed_model` is different, newest first, one at a time. Nothing else needs to run.

- An old clip stays `done` and searchable by its old transcript until the new one replaces it. `/findtheclimp` never loses it.
- New posts always go first: the worker only takes an old clip when nothing new is pending.
- If a clip can't be redone, it keeps its old transcript, with the error in `last_error`. After `MAX_ATTEMPTS` (or straight away for a permanent error, such as a file that's too big), it isn't tried again.
- Set `RETRANSCRIBE=false` to leave old clips alone, for example while trying out a model.

Clips with no `transcribed_model` (transcribed before the column existed) are never redone automatically, because nobody knows which model made them. Label them yourself. If they were all made with the current model, mark them as such; to have the worker redo them, give them any other label:

```sql
-- Made with the current model: nothing to redo.
UPDATE clips SET transcribed_model = 'small.en' WHERE status = 'done' AND transcribed_model IS NULL;
-- Or, to redo them:
UPDATE clips SET transcribed_model = 'unknown' WHERE status = 'done' AND transcribed_model IS NULL;
```

Clips brought over by `yks-import-mongo` are labelled `yks-smart-bot`, so they're redone automatically.

To check progress:

```sql
SELECT transcribed_model, count(*) FROM clips WHERE status = 'done' AND NOT deleted GROUP BY 1;
-- redos that gave up (they keep their old transcript):
SELECT id, attempts, last_error FROM clips
WHERE status = 'done' AND transcribed_model <> 'small.en' AND last_error IS NOT NULL;
-- try those again:
UPDATE clips SET attempts = 0, retry_at = NULL, last_error = NULL
WHERE status = 'done' AND transcribed_model <> 'small.en' AND last_error IS NOT NULL;
```
