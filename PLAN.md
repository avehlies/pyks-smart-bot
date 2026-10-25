# pyks-smart-bot — Python rewrite plan

Port of [`avehlies/yks-smart-bot`](https://github.com/avehlies/yks-smart-bot) (TypeScript, discord.js v13 + discord-akairo + MongoDB) to Python.

## Decisions

| Area | Decision |
| --- | --- |
| Discord library | **discord.py ≥ 2.7** (`discord.py[voice]` + `davey`). 2.7 adds DAVE (E2EE voice), which Discord has required since March 2026. |
| Commands | Keep the `!` prefix commands with the same aliases. Mentioning the bot also works as a prefix and names are case-insensitive. (Akairo's `aliasReplacement` only expanded aliases that contain hyphens; none do, so it isn't ported.) Regex easter eggs stay as message listeners. `/findtheclimp` stays the only slash command. |
| Database | **Postgres** with SQLAlchemy 2 (async) + asyncpg, and **Alembic** for migrations. Real tables replace Akairo's JSON-string blobs. A one-time import script brings over the Mongo data. |
| Clip pipeline | A **separate `clip-worker` application** (its own package, image and container) downloads, transcribes and indexes clips. The bot records clips, backfills history and serves `!clip` / `/findtheclimp`, but contains nothing related to transcription. They coordinate through Postgres: a job-status column, `FOR UPDATE SKIP LOCKED`, and `LISTEN/NOTIFY` for instant pickup. |
| Transcription | `faster-whisper` (`tiny.en` by default, configurable), in the separate clip-worker application. Replaces `@xenova/transformers`. |
| Search | Postgres full-text search: a generated `tsvector` column + GIN index, queried with `websearch_to_tsquery` + `ts_rank`. |
| HTTP | `aiohttp` web server inside the bot process (it needs the gateway client to post). Only `/api/share-clip` is ported. |
| Dropped (backlog) | `!kickstarter`, `!realorfake`, `!cassie` (already commented out), `!episode` (its OpenAI curie fine-tune was retired), the React/static site, the `sessions` collection. |
| Tooling | Python 3.12, `uv`, `ruff`, `pytest` (+ `pytest-asyncio`), Docker + docker-compose (`postgres`, `bot`, `clip-worker`). |

## Layout

```
pyks-smart-bot/
  pyproject.toml            # the bot package + uv workspace root
  docker-compose.yml        # postgres, bot, clip-worker
  Dockerfile                # the bot image
  alembic.ini
  migrations/
  assets/                   # images copied from the TS repo (only the ones still used)
  clip-worker/              # transcription sidecar: own pyproject, Dockerfile, tests
  src/yks/
    config.py               # pydantic-settings, reads .env
    log.py
    db/                     # engine/session, models, small repository helpers
    feeds.py                # RSS fetch + parse with a short TTL cache
    bot/
      __main__.py  client.py
      checks.py             # per-user cooldowns + per-guild "global rate" gate
      web.py                # /api/share-clip
      cogs/                 # one module per feature (below)
  scripts/migrate_from_mongo.py
  tests/
```

## Database schema (initial)

| Table | Columns | Replaces |
| --- | --- | --- |
| `guild_settings` | `guild_id` PK, `welcome_disabled` bool, `rss_channel_id` bigint null | settings `welcomeMsgDisabled`, `rssChannel` (stored the whole channel as JSON) |
| `welcomed_members` | (`guild_id`, `user_id`) PK, `welcomed_at` | `welcomedMembers` array |
| `bot_state` | `key` PK, `value` text | `latestMainEpTitle`, `latestBonusEpTitle`, `latestEpNum` (stored under the bot's user ID) |
| `best_episode_votes` | (`guild_id`, `user_id`) PK, `episode` int, `voted_at` | `bestEpByUser` + `bestEpTotals` (totals are now a `GROUP BY`) |
| `reaction_tallies` | `message_id` PK, `kind` (`mailbag`/`kickstarter`), `guild_id`, `channel_id`, `count`, `message_created_at` | `mailbagMessages`, `kickstarters` (arrays of JSON strings) |
| `clips` | `id` PK, `guild_id`, `channel_id`, `message_id`, `attachment_id`, `filename`, `content_type`, `url`, `deleted`, `transcription`, `status` (`pending`/`processing`/`done`/`failed`), `attempts`, `last_error`, `claimed_at`, `search` tsvector GENERATED + GIN, `created_at`; unique (`message_id`, `attachment_id`) | `clips` (one doc per message; with several attachments, the last one overwrote the others) |

## Phases

Each phase ends with something runnable that can be tested on a test guild before moving on.

### Phase 0 — Foundations ✅
- `pyproject.toml`, ruff/pytest config, `.env.example` (same variable names as the TS bot; `DATABASE_URL` replaces `MONGO_*`).
- Config, logging, async engine, Alembic, and the initial migration with every table above.
- Bot client: intents (guilds, members, messages, message content, reactions, voice states); prefix `!` or a mention; case-insensitive; owner ID `293571605081358336`.
- `checks.py`: an Akairo-style per-user cooldown (`ratelimit` uses per `cooldown` window, default 1 s for every command) and a `guild_gate(key, ttl)` that replaces `globalRates`.
- Global error handler (cooldown and check failures are silent, like the TS bot), plus an empty, configurable user blocklist (the old blacklist inhibitor).
- Dockerfile + docker-compose with Postgres. **Done when:** `docker compose up` migrates the DB and the bot comes online. Owner-only `!ping` reports gateway and database latency.

### Phase 1 — Easter eggs and simple commands ✅
- All 24 `simpleCommands` entries, as a Python data table (`yks/bot/simple_commands.py`): text / reply / embed / file / random file / random text, alias or regex triggers, per-user cooldowns, guild gate.
- A message-trigger engine (`yks/bot/triggers.py`) replaces Akairo's regex commands: it runs only on messages that aren't valid commands, every matching trigger fires, and each has its own per-user cooldown.
- `!burgy` (days since 2021-04-07; on April 7 its cooldown and `!mug`'s drop to the 1 s default), `!emma` (random guild emoji), `hello` (only when the bot is mentioned), the `the … store` reaction, `!help`.
- `BOT_TIMEZONE` (default `America/Chicago`) decides what "today" is.

### Phase 2 — Guild settings and member welcome ✅
- `!welcome` toggle (needs Manage Roles), `!rssChannel <#channel | id | name>` (administrators or user `329288617564569602`), `!newmember <id>` (owner only).
- `on_member_join`: each member is welcomed at most once per guild (an atomic insert into `welcomed_members`). The welcome image is built with **Pillow**: avatar clipped to a circle, green multi-line text that shrinks to fit (long lines squeezed to 550 px like canvas `maxWidth`), DejaVu Sans bundled in `assets/fonts/`. It posts to the system channel with one of the nine original greetings.
- Rendering runs in a worker thread so it doesn't block the bot.

### Phase 3 — Podcast feeds ✅
- `yks/feeds.py`: `httpx` + `feedparser`, parsed into `Feed`/`Episode` objects, with a 15 s cache per URL (concurrent requests share one download). The TS bot re-downloaded both feeds on every command.
- `!latest [main|bonus|premium|patreon|both]` (aliases `!eps`, `!recent`), `!today`, `!best [episode]` (vote, change a vote, top 10 with random tie-breaks, the Episode 101 joke).
- A watcher every 20 s: detects new main/bonus episodes, sets the "Listening to …" presence, and announces in each guild's RSS channel with the 1ohno/2they/3did/40another/yks reactions. Public episodes cross-posted to Patreon are announced once.

### Phase 4 — Reaction tallies ✅
- `on_raw_reaction_add/remove` (raw events also cover uncached messages, replacing partials): `:this:` reactions in the mailbag channel, and on the bot's own kickstarter embeds (with a footer) in the kickstarter channel.
- Each event re-reads the message's real `:this:` count from Discord and stores it (`reaction_tallies`), deleting the row at zero.
- `!topmail` (aliases `!topmailbag`, `!bestmailbag`, `!bestmail`): top 10 from the last 30 days, skipping the bot's own messages, 1024-character limit. `!topks` (aliases `!topkickstarters`, `!bestkickstarters`, `!bestks`).
- Daily task (09:00 UTC, and once at startup) that deletes mailbag tallies older than 30 days.

### Phase 5 — Clips (bot side) ✅
- `on_message` in the clip channel records a `pending` row per audio/video attachment (skipping the bot itself and user `789204496097214514`). The database trigger sends `NOTIFY clips_new` for the worker. Deleting a message flags its clips `deleted`.
- `!clip` / `!climp` (and `!clips`, `!randomclip`, `!climps`, `!randomclimp`): picks a random clip, fetches its message from the clip channel, and re-uploads the attachment. Files too big for the bot to upload are posted as a link. Deleted messages are flagged and another clip is picked.
- History backfill lives in the bot (recording clips is a bot job): `GATHER_ALL_CLIPS=true` on startup, as before, or the owner-only `!backfillclips`.
- `POST /api/share-clip` (Basic auth with `BOT_AUTH_USERNAME`/`BOT_AUTH_PASSWORD`; JSON `userId`, `page`, `url`) posts to the clip channel and returns a proper status. `GET /healthz` is used by the Docker healthcheck.

### Phase 6 — Clip worker sidecar ✅
- A **separate application** in `clip-worker/` (package `yks_clip_worker`, a uv workspace member). It has its own Dockerfile, its own compose service, and its own dependencies (asyncpg, httpx, faster-whisper). It imports no bot code and talks to the `clips` table with SQL; the bot has no transcription code or settings at all.
- A loop that waits on `LISTEN clips_new` (with a polling fallback), claims the newest pending clip with `SKIP LOCKED`, and puts stuck `processing` jobs back after a timeout (or right away on SIGTERM).
- **Refreshes the attachment URL through the Discord REST API before downloading**, since stored CDN links expire. Deleted messages are marked `skipped`.
- Pipeline: download (httpx, streamed to a temp dir, size-capped) → faster-whisper (decodes audio and video via PyAV; VAD filter; `tiny.en` by default) → save the transcript. Retries back off (new `retry_at` column) and end in `failed` with `last_error`.
- CI builds both images and checks that each contains only its own application.

### Phase 7 — `/findtheclimp` ✅
- Autocomplete: at least 3 characters → ranked Postgres full-text search over transcribed, non-deleted clips (falling back to a substring match when the phrase is all stop words), duplicate transcripts removed, top 20, a 94-character snippet centred on the match. The choice value is the clip ID.
- A private (ephemeral) preview with **Post it** / **Don't post it** buttons (`discord.ui.View`, only the requester can press them). Posting sends the clip to the clip channel with a "Requested by … using the `/findtheclimp` command for "…"" embed.
- Slash commands are registered on demand with the owner-only `!sync` (global) or `!sync here` (this server, instantly), instead of deleting and re-registering every command on each start as the TS bot did.

### Phase 8 — Listen together ✅
- `!listen [play [n] | random | pause | stop]`: joins the member's voice channel, downloads the episode to a temp file (so pausing can't drop a stream), and plays it with `FFmpegOpusAudio` (ffmpeg does the Opus encoding; discord.py ≥ 2.7 + `davey` handle DAVE encryption).
- One session per server (the TS bot had a single global player). A wrapper `AudioSource` counts 20 ms frames to track the playback position, and a "Now playing" embed's progress bar updates every 10 s.
- ⏸ ⏹ ▶️ reaction controls on the Now playing message; stop asks ✅/❌ with a 10 s timeout, and only the person who reacted can answer. Status updates ("Paused at …", "Resuming.", "Finished playing episode.") go into one reply that's edited in place. Temp files and the voice connection are cleaned up on finish, error or failed download.
- `!listen url <link>` (play any audio URL) is owner-only.

### Phase 9 — Data migration and cutover ✅
- `yks-import-mongo` (`src/yks/tools/mongo_import.py`, optional `migrate` extra for pymongo) reads the Akairo `models` docs and the `clips` collection. It decodes the JSON-string values (the `rssChannel` channel dump, the `bestEpByUser` map entries, the tally JSON strings) and upserts idempotently. `--dry-run` shows counts and warnings first.
- Clips: the channel, attachment ID and filename come from the stored URL. Existing transcripts are kept, so nothing is re-transcribed. Empty transcripts (the TS bot's failure marker) are queued again unless `--keep-empty-transcripts` is passed. Rows the new bot already recorded get the old transcript without overwriting newer worker output.
- Tallies get their channel from the configured IDs, and a timestamp from `ts` or, failing that, the message's snowflake.
- The cutover checklist and rollback are in the README ("Moving over from the TypeScript bot").

### Backlog
- Revive `!kickstarter` / `!realorfake` / `!episode` on a current LLM API (and product images).
- `!cassie` (Unsplash frog).
- A web front end for clips (`im-at.work/clips`).

## Fixes made during the port (rather than copied as-is)

- `!today` drops every bonus episode (its "not in main feed" filter is inverted).
- `!listen random` picked an array index, not an episode, and its skip-101 logic was off by one; it now picks a random numbered episode.
- `!listen url` let anyone make the bot download and play any URL; it's now owner-only.
- The "Now playing" embed crashed on titles like "Ep 410: …" (it only understood "Episode N"); both forms work now.
- The April 7 "no cooldown" exemption for `!mug`/`!burgy` is calculated only once at process start.
- The kickstarter reaction listener crashes on messages without embeds.
- Each clip message keeps only its last attachment in the DB.
- `!clip` fetched the clip's message from whatever channel the command was used in, so it only worked inside the clip channel; it now always reads from the clip channel. It re-uploads only files the bot is allowed to upload and links bigger ones, and stops after 10 picks instead of making up to 100 blind fetches.
- `/api/share-clip` never answered the HTTP request (callers hung until timeout); it now returns 200/400/401/502/503 JSON. Credentials are compared in constant time, and `userId` must be numeric (it's put into a mention).
- Every slash command is deleted and re-registered on each startup. Commands are now synced on demand with `!sync`.
- `/findtheclimp` fetched the clip's message from the channel the command was used in, so a clip could only be previewed and posted from inside the clip channel.
- `/findtheclimp` found nothing for phrases made only of stop words; it now falls back to a substring match.
- `!help` still lists the disabled kickstarter commands.
- The `hello` trigger used up its 1-hour cooldown on *any* message containing "hi"/"sup"/etc. (even "this"), so a later real greeting was ignored. Now the cooldown only counts when the bot is mentioned.
- `!emma` sometimes sent nothing when one of its five emojis was missing; it now picks from the ones that exist.
- Three `!staysippin` photos were 10–13 MB phone originals, over Discord's 10 MB bot upload limit (on servers without enough boosts). They're downscaled to 2048 px.
- `!staysippin` uploaded some JPEGs under `.png` names; files now keep their real names.
- `!rssChannel` stored a JSON dump of the whole channel object; it now stores the channel ID.
- Guilds without a system channel made the welcome listener fail; it now skips them, with a log line.
- `!today`'s bonus filter was inverted, so bonus episodes never appeared. It also sent every match in one message, which Discord rejects past 10 embeds or 6,000 characters; results are now split across messages. Bonus episodes link as "Patreon" and main ones as "Listen" (the TS bot labelled every link "Patreon").
- `!today` compares dates in `BOT_TIMEZONE` (the TS bot used the server's clock, UTC in Docker), so a late-evening release counts for the right day.
- A feed outage crashed `!latest`, `!today`, `!best` and the watcher; they now reply with "Couldn't reach the podcast feed right now", or skip that poll.
- The "Listening to …" presence was only set when a new bonus episode appeared, so it vanished after every restart; it's now set on the first poll.
- On a fresh database the watcher announced the current episodes as new on first start; it now just records them. (After the Phase 9 data import, the old "last seen" titles carry over anyway.)
- `!best` read the newest episode number from the first feed item only, so an unnumbered bonus item at the top made every new vote "Not an episode."; it now uses the highest numbered episode. It also says "1 vote", not "1 votes".
- The internal `newEp` argument of `!latest` is no longer user-visible (anyone could trigger the announcement reactions).
- Reaction tallies were +1/-1 counters, so events missed while the bot was down left them wrong forever; they now store the real count read from Discord on every event.
- The kickstarter reaction listener crashed on messages without embeds; `!topmail`/`!topks` crashed when a tallied message had been deleted. Deleted messages now drop out of the tallies.
- `!topmail` relied solely on the daily cleanup for its 30-day window; it now also filters by date when listing, and says so when there's nothing to show.

## Environment variables

`AUTH_TOKEN`, `DATABASE_URL`, `YKS_GUILD_ID`, `YKS_CLIP_CHANNEL_ID`, `YKS_MAILBAG_CHANNEL_ID`, `YKS_KICKSTARTER_BOT_CHANNEL_ID`, `MAIN_FEED_RSS`, `BONUS_FEED_RSS`, `BOT_AUTH_USERNAME`, `BOT_AUTH_PASSWORD`, `PORT` (default 3000), `GATHER_ALL_CLIPS`, `BOT_TIMEZONE`, `MONGO_URL` (migration script only). Clip worker: `WHISPER_MODEL` (default `tiny.en`), `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`, `MAX_ATTEMPTS` (see `clip-worker/README.md`).
