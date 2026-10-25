# pyks-smart-bot — Python rewrite plan

Port of [`avehlies/yks-smart-bot`](https://github.com/avehlies/yks-smart-bot) (TypeScript, discord.js v13 + discord-akairo + MongoDB) to Python.

## Decisions

| Area | Decision |
| --- | --- |
| Discord library | **discord.py ≥ 2.7** (`discord.py[voice]` + `davey`). 2.7 adds DAVE (E2EE voice), which Discord has required since March 2026. |
| Commands | Keep the `!` prefix commands with the same aliases. Mentioning the bot also works as a prefix and names are case-insensitive. (Akairo's `aliasReplacement` only expanded aliases that contain hyphens; none do, so it isn't ported.) Regex easter eggs stay as message listeners. `/findtheclimp` stays the only slash command. |
| Database | **Postgres** with SQLAlchemy 2 (async) + asyncpg, and **Alembic** for migrations. Real tables replace Akairo's JSON-string blobs. A one-time import script brings over the Mongo data. |
| Clip pipeline | A **separate `clip-worker` process** (its own container) downloads, transcribes and indexes clips. The bot only records that a clip exists. They coordinate through Postgres: a job-status column, `FOR UPDATE SKIP LOCKED`, and `LISTEN/NOTIFY` for instant pickup. |
| Transcription | `faster-whisper` (`tiny.en` by default, configurable) + ffmpeg. Replaces `@xenova/transformers`. |
| Search | Postgres full-text search: a generated `tsvector` column + GIN index, queried with `websearch_to_tsquery` + `ts_rank`. |
| HTTP | `aiohttp` web server inside the bot process (it needs the gateway client to post). Only `/api/share-clip` is ported. |
| Dropped (backlog) | `!kickstarter`, `!realorfake`, `!cassie` (already commented out), `!episode` (its OpenAI curie fine-tune was retired), the React/static site, the `sessions` collection. |
| Tooling | Python 3.12, `uv`, `ruff`, `pytest` (+ `pytest-asyncio`), Docker + docker-compose (`postgres`, `bot`, `clip-worker`). |

## Layout

```
pyks-smart-bot/
  pyproject.toml            # one package, two entry points: yks-bot, yks-clip-worker
  docker-compose.yml        # postgres, bot, clip-worker
  Dockerfile                # shared base; worker installs the [worker] extra
  alembic.ini
  migrations/
  assets/                   # images copied from the TS repo (only the ones still used)
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
    clipworker/
      __main__.py           # run loop + `backfill` subcommand
      queue.py  discord_rest.py  media.py  transcribe.py
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

### Phase 1 — Easter eggs and simple commands
- Port all 25 `simpleCommands` entries into a Python data table (text / reply / embed / file / random file / random text, with alias or regex triggers, per-user cooldowns, guild gate).
- `!burgy` (days since 2021-04-07; no cooldown on April 7), `!emma` (random guild emoji), `hello` (only when the bot is mentioned), `the … store` reaction, `!help` (update the text to drop the retired kickstarter commands).

### Phase 2 — Guild settings and member welcome
- `!welcome` toggle (Manage Roles), `!rssChannel #channel` (admin or user `329288617564569602`), `!newmember <id>` (owner only).
- `on_member_join`: welcome each member only once; build the welcome image with **Pillow** (avatar clipped to a circle, auto-shrinking multi-line green text, bundled DejaVu font); random greeting line; post to the system channel.

### Phase 3 — Podcast feeds
- `feeds.py`: `httpx` + `feedparser` with a ~15 s cache (the TS bot re-downloads both feeds on every command).
- `!latest [main|bonus|premium|patreon|both]`, `!today`, `!best [episode]` (vote, change a vote, top 10, random tie-break, the 101 joke).
- Background task every 20 s: detect new main/bonus episodes, set the "Listening to …" presence, announce in each guild's RSS channel with the 1ohno/2they/3did/40another/yks reactions.

### Phase 4 — Reaction tallies
- `on_raw_reaction_add/remove` (the raw events handle uncached messages, replacing partials): `:this:` reactions in the mailbag channel, and on the bot's own embeds in the kickstarter channel.
- `!topmail` (top 10 from the last 30 days, skipping the bot's own messages, 1024-character limit), `!topks`.
- Daily task that deletes mailbag tallies older than 30 days.

### Phase 5 — Clips (bot side)
- `on_message` in the clip channel: insert a `pending` row for each audio/video attachment, skipping the bot itself and user `789204496097214514`, then `NOTIFY clips_new`.
- `!clip` / `!climp` etc.: pick a random non-deleted clip, fetch the message, post a fresh attachment URL; if the message is gone (404), mark the clip deleted and retry (instead of up to 100 blind attempts).
- `POST /api/share-clip` (Basic auth with `BOT_AUTH_USERNAME`/`BOT_AUTH_PASSWORD`; body `userId`, `page`, `url`) posts to the clip channel.

### Phase 6 — Clip worker sidecar
- A loop that waits on `LISTEN clips_new` (with a polling fallback), claims one job at a time with `SKIP LOCKED`, and puts stuck `processing` jobs back after a timeout.
- **Refreshes the attachment URL through the Discord REST API before downloading.** Discord CDN links are signed and expire, and the TS bot stored them for later downloads, so old backfills could fail.
- Pipeline: download (httpx, streamed to a temp dir) → ffmpeg to 16 kHz mono WAV → faster-whisper → save the transcript. `failed` after N attempts, with the error recorded.
- `yks-clip-worker backfill`: page through the clip channel's full history over REST and add the rows the bot missed (replaces `GATHER_ALL_CLIPS=true`).
- A separate image with the `[worker]` extra, so the bot image stays small. Controlled by `TRANSCRIBE_CLIPS` and `WHISPER_MODEL`.

### Phase 7 — `/findtheclimp`
- Autocomplete: at least 3 characters → ranked full-text search, duplicate transcripts removed, top 20, a 94-character snippet centred on the match; the choice value is the clip ID.
- Ephemeral preview with **Post it** / **Don't post it** buttons (`discord.ui.View`); posting sends the clip to the clip channel with a "Requested by … for "…"" embed.
- Sync slash commands on demand (owner `!sync`), instead of deleting and re-registering every command on each start as the TS bot does.

### Phase 8 — Listen together
- `!listen [play [n] | random | pause | stop]`: join the member's voice channel, download the episode to a temp file, play it with `FFmpegPCMAudio`.
- A wrapper `AudioSource` counts 20 ms frames to track the playback position (discord.py doesn't expose one); a "Now playing" embed whose progress bar updates every 10 s.
- ⏸ ⏹ ▶️ reaction controls; stop asks ✅/❌ with a 10 s timeout; temp files and the voice connection are cleaned up on finish or error.

### Phase 9 — Data migration and cutover
- `scripts/migrate_from_mongo.py`: read the Akairo `models` docs and the `clips` collection, parse the JSON-string values, write to Postgres idempotently. Existing transcriptions are kept, so nothing needs re-transcribing.
- Cutover checklist: stop the TS bot → run the migration → start the compose stack → spot-check each command.

### Backlog
- Revive `!kickstarter` / `!realorfake` / `!episode` on a current LLM API (and product images).
- `!cassie` (Unsplash frog).
- A web front end for clips (`im-at.work/clips`).

## Fixes made during the port (rather than copied as-is)

- `!today` drops every bonus episode (its "not in main feed" filter is inverted).
- `!listen random` picks an array index, not an episode number, and its skip-101 logic is off by one.
- The April 7 "no cooldown" exemption for `!mug`/`!burgy` is calculated only once at process start.
- The kickstarter reaction listener crashes on messages without embeds.
- Each clip message keeps only its last attachment in the DB.
- Every slash command is deleted and re-registered on each startup.
- `!help` still lists the disabled kickstarter commands.

## Environment variables

`AUTH_TOKEN`, `DATABASE_URL`, `YKS_GUILD_ID`, `YKS_CLIP_CHANNEL_ID`, `YKS_MAILBAG_CHANNEL_ID`, `YKS_KICKSTARTER_BOT_CHANNEL_ID`, `MAIN_FEED_RSS`, `BONUS_FEED_RSS`, `BOT_AUTH_USERNAME`, `BOT_AUTH_PASSWORD`, `PORT` (default 3000), `TRANSCRIBE_CLIPS`, `WHISPER_MODEL` (default `tiny.en`), `MONGO_URL` (migration script only).
