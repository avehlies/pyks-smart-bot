# Deploying pyks-smart-bot (and moving over from the TypeScript bot)

This is the complete runbook: preparing the server, moving the data out of MongoDB, switching from the TypeScript bot to this one, checking that everything works, and running it afterwards. Follow it top to bottom.

- **Parts 1–3** can be done any time beforehand. The old bot keeps running and nobody notices.
- **Part 4** is the switch-over. Expect about 5–10 minutes when neither bot is online.
- **Parts 5–7** cover after the switch, rolling back, and fixing problems.

All commands run on the server, from the repository folder, unless a step says otherwise.

## What will be running

| Service | What it is | Memory (approx.) |
| --- | --- | --- |
| `postgres` | The database: Postgres 17, data kept in the `pgdata` volume. Reachable only from the server itself (`127.0.0.1:5432`). | 50–100 MB |
| `migrate` | Applies database migrations, then exits. Runs before the bot starts, every time. | — |
| `bot` | The Discord bot, plus its HTTP API (`/api/share-clip`, `/healthz`) on `PORT`. | 100–150 MB, +50 MB during `!listen` |
| `clip-worker` | The separate transcription app. Its Whisper model loads only while there are clips to transcribe. | ~40–60 MB idle, ~200–350 MB while transcribing |

---

## Part 1 — Prepare the server

### 1.1 Check the requirements

- **A Linux server with Docker Engine and Docker Compose v2.24 or newer** (the compose file uses `env_file: required: false`). Check with:
  ```sh
  docker --version
  docker compose version
  ```
- **Disk:** about 3 GB free, covering the images (about 0.5 GB for the bot and 0.6 GB for the clip worker, plus build cache), the model (75 MB for `tiny.en`) and the database.
- **Memory:** about 1 GB free is comfortable.
- **Outbound HTTPS** to:
  - `discord.com`, `gateway.discord.gg`, `cdn.discordapp.com` and `media.discordapp.net` (Discord)
  - your podcast feed hosts (`MAIN_FEED_RSS` / `BONUS_FEED_RSS`)
  - `huggingface.co`, for the clip worker's first model download
  - your MongoDB host, for the import only
  - Docker Hub, `ghcr.io` and `pypi.org`, while building the images
- **Ports:** `PORT` (3000 by default) must be free on the host. If the TypeScript bot runs on the same server it probably uses 3000 too; that's fine, because it will be stopped before the new bot starts (step 4.4).

### 1.2 Get the code

```sh
git clone https://github.com/avehlies/pyks-smart-bot.git
cd pyks-smart-bot
git checkout main
git pull
```

### 1.3 Check the Discord application

The new bot uses **the same Discord application and token** as the TypeScript bot, so it's already in the server with its permissions. Still, check these in the [Discord Developer Portal](https://discord.com/developers/applications) → your application → **Bot**:

1. **Privileged Gateway Intents:**
   - **Server Members Intent: ON.** Welcome messages and `!newmember` need it.
   - **Message Content Intent: ON.** `!` commands and the easter eggs need it.
   - Presence Intent isn't needed.
2. **Copy the token**, or reuse the old bot's `AUTH_TOKEN`. If you ever reset the token, the old bot stops working immediately.

The bot needs these permissions in the server. The old bot already had them, so this is only for troubleshooting:
- View Channels
- Send Messages
- Embed Links
- Attach Files
- Add Reactions
- Read Message History
- Use External Emojis
- Use Application Commands
- In voice channels, for `!listen`: Connect and Speak

If you re-invite the bot, use the scopes `bot` and `applications.commands`.

### 1.4 Find your Discord user ID

The owner-only commands (`!sync`, `!ping`, `!newmember`, `!backfillclips`) and the owner's freedom from cooldowns go to `OWNER_ID`. It defaults to the TypeScript bot's hard-coded owner, `293571605081358336`. If that isn't you, get your ID: Discord → Settings → Advanced → turn on **Developer Mode**, then right-click your name → **Copy User ID**.

---

## Part 2 — Configure

### 2.1 Create `.env`

```sh
cp .env.example .env
chmod 600 .env        # it holds the bot token and passwords
```

### 2.2 Fill it in

Most values carry over from the TypeScript bot's environment under the **same names**:

| New `.env` variable | Value | From the TypeScript bot |
| --- | --- | --- |
| `AUTH_TOKEN` | Bot token | `AUTH_TOKEN` (same) |
| `OWNER_ID` | Your Discord user ID (step 1.4), or leave blank for the default | (was hard-coded) |
| `YKS_GUILD_ID` | The YKS server's ID | `YKS_GUILD_ID` (same) |
| `YKS_CLIP_CHANNEL_ID` | The clip channel's ID | `YKS_CLIP_CHANNEL_ID` (same) |
| `YKS_MAILBAG_CHANNEL_ID` | The mailbag channel's ID | `YKS_MAILBAG_CHANNEL_ID` (same) |
| `YKS_KICKSTARTER_BOT_CHANNEL_ID` | The kickstarter-bot channel's ID | `YKS_KICKSTARTER_BOT_CHANNEL_ID` (same) |
| `YKS_FOOD_CHANNEL_ID` | The food channel's ID (`682325132127371341`), where recipe links get a nyetcook.ing reply | (new) |
| `MAIN_FEED_RSS` | Main podcast feed URL | `MAIN_FEED_RSS` (same) |
| `BONUS_FEED_RSS` | Patreon feed URL | `BONUS_FEED_RSS` (same) |
| `BOT_AUTH_USERNAME` / `BOT_AUTH_PASSWORD` | Credentials for `/api/share-clip` | Same names. Keep the same values so the im-at.work site keeps working. |
| `PORT` | Host port for the HTTP API | `PORT` (same) |
| `POSTGRES_PASSWORD` | **A new, strong password** (see 2.3) | (new) |
| `MONGO_URL` | Connection string for the old database, only for the import (see 2.4) | Built from `MONGO_HOST`, `MONGO_PORT`, `MONGO_USER`, `MONGO_PW`, `MONGO_DB` |
| `BOT_TIMEZONE` | Timezone for `!today` and the April 7 Burgy-day rule. Default `America/Chicago`. | (the TS bot used the server clock) |
| `WHISPER_MODEL` | `tiny.en` (recommended; see the clip worker's README for others) | (was hard-coded) |
| `WHISPER_IDLE_UNLOAD_SECONDS` | `300`: free the model after 5 idle minutes | (new) |
| `GATHER_ALL_CLIPS` | Leave `false` (use `!backfillclips` later if needed) | `GATHER_ALL_CLIPS` (same meaning) |
| `BLOCKED_USER_IDS` | Comma-separated user IDs to ignore (optional) | (was an empty list in code) |

Leave `DATABASE_URL` as it is. Docker Compose sets the right one for its containers, and the line in `.env` is only for running the bot outside Docker.

`TRANSCRIBE_CLIPS` and `MONGO_HOST`, `MONGO_PORT`, `MONGO_USER`, `MONGO_PW` aren't used any more and can stay or go. `MONGO_DB` is still read by the importer, as the database name, if your `MONGO_URL` doesn't include one.

### 2.3 Choose the Postgres password now

Set `POSTGRES_PASSWORD` **before the first start**. Postgres only reads it when it creates the `pgdata` volume, so changing it in `.env` later doesn't change the real password, and the bot can no longer log in. If you need to change it later, see Part 7.

### 2.4 Build `MONGO_URL`

Build it from the old bot's settings, which match how the TypeScript bot connected:

- **MongoDB Atlas** (`MONGO_HOST` contains `mongodb.net`):
  ```
  MONGO_URL=mongodb+srv://MONGO_USER:MONGO_PW@MONGO_HOST/MONGO_DB?retryWrites=true&w=majority
  ```
- **Self-hosted MongoDB:**
  ```
  MONGO_URL=mongodb://MONGO_USER:MONGO_PW@MONGO_HOST:MONGO_PORT/MONGO_DB?authSource=admin
  ```

If the password contains special characters (`@ : / ? # [ ] %`), percent-encode them; for example `@` becomes `%40`.

### 2.5 Let the server reach MongoDB

If the old database is on **MongoDB Atlas**, add the new server's public IP under Atlas → your project → **Network Access** → **IP Access List**. Without it, the import times out. To find the IP, run `curl -s https://ifconfig.me` on the server. You can remove the entry after the switch-over.

---

## Part 3 — Build and rehearse (the old bot keeps running)

### 3.1 Build the images

```sh
docker compose build
```

This takes several minutes the first time; the clip worker's speech libraries are large.

### 3.2 Start only the database and apply the migrations

```sh
docker compose up -d postgres migrate
docker compose ps -a
docker compose logs migrate
```

`postgres` should be **healthy**, and `migrate` should show **Exited (0)**. Its log should end with:
```
Running upgrade  -> fb111a7391c0, initial schema
Running upgrade fb111a7391c0 -> 0c4342cb8e82, clip retry_at
```

### 3.3 Dry-run the import

This reads MongoDB and reports what it would import, but writes nothing:

```sh
docker compose run --rm bot uv run --no-sync --with "pymongo>=4.10" yks-import-mongo --dry-run
```

(The first run downloads pymongo into the container; that's expected.)

Expected output looks like:
```
Read N settings document(s) and M clip(s) from MongoDB.
Would import 1 guild settings
Would import … welcomed members
Would import 3 bot state values
Would import … best-episode votes
Would import … reaction tallies
Would import … clips
Would import … clips with transcripts
```

Check:
- `Read … settings document(s)` should be at least **2**: the YKS server, plus the bot's own document holding the latest episode titles.
- `bot state values` should be **3**. These are the latest main title, the latest bonus title and the latest episode number. They stop the new bot from announcing the current episodes as new.
- `clips` should be close to the number of clips the old bot knew about.
- **Warnings**:
  - `Skipped clip …: unrecognised URL` for a handful of clips is fine; those had no Discord attachment URL.
  - `YKS_GUILD_ID is not set` or `No channel ID configured for …` means a value is missing in `.env`. Fix it and run again.

If it fails to connect, see Part 7.

### 3.4 Optional: do a rehearsal import

Importing now lets you look at the data and time the import. It's safe: MongoDB is only read, and the old bot never sees Postgres.

```sh
docker compose run --rm bot uv run --no-sync --with "pymongo>=4.10" yks-import-mongo
```

Then check the counts (see 4.6 for more queries):

```sh
docker compose exec postgres psql -U yks -d yks -c "SELECT status, count(*) FROM clips GROUP BY status;"
```

You'll repeat the import during the switch-over to pick up anything that changed in the meantime.

---

## Part 4 — Switch over (5–10 minutes with no bot)

### 4.1 Pick a quiet time

Optionally, post a heads-up in the server.

### 4.2 Pull any last changes

```sh
git pull
docker compose build
```

Skip this if nothing changed since Part 3.

### 4.3 Check the database is up

```sh
docker compose up -d postgres migrate
```

### 4.4 Stop the TypeScript bot

**Do this before starting the new bot.** Both use the same token, so if both run, every command gets answered twice and the reaction tallies get counted by both.

How depends on how the old bot runs:
- Docker: `docker ps`, then `docker stop <container>`. Use `docker update --restart=no <container>` too, so it doesn't come back after a reboot.
- pm2: `pm2 stop yks-smart-bot && pm2 save`.
- systemd: `sudo systemctl stop <service> && sudo systemctl disable <service>`.

Confirm in Discord that the bot shows as **offline** in the member list.

### 4.5 Run the final import

The old bot is stopped, so nothing in MongoDB changes from here.

**If you did the rehearsal (3.4),** clear the tables first, so the final import is an exact copy. Nothing else has written to them yet.
```sh
docker compose exec postgres psql -U yks -d yks -c "TRUNCATE guild_settings, welcomed_members, bot_state, best_episode_votes, reaction_tallies, clips RESTART IDENTITY;"
```

Then import:
```sh
docker compose run --rm bot uv run --no-sync --with "pymongo>=4.10" yks-import-mongo
```

The output is the same as the dry run, with `Imported` in place of `Would import`.

### 4.6 Check the imported data

```sh
docker compose exec postgres psql -U yks -d yks
```

```sql
-- Latest-episode memory: should show 3 rows.
SELECT key, value FROM bot_state;

-- Server settings: the welcome toggle, and the RSS channel ID that !rssChannel set in the old bot.
SELECT * FROM guild_settings;

-- Counts.
SELECT 'welcomed members' AS what, count(*) FROM welcomed_members
UNION ALL SELECT 'best-episode votes', count(*) FROM best_episode_votes
UNION ALL SELECT 'reaction tallies', count(*) FROM reaction_tallies
UNION ALL SELECT 'clips', count(*) FROM clips;

-- Clips by status. "done" ones already have transcripts. "pending" ones are queued for the worker:
-- they had no transcript, or an empty one, in the old bot.
SELECT status, deleted, count(*) FROM clips GROUP BY status, deleted ORDER BY status;

-- Top !best episodes, to compare with the old bot's !best listing.
SELECT episode, count(*) AS votes FROM best_episode_votes GROUP BY episode ORDER BY votes DESC LIMIT 10;
```

Type `\q` to leave psql.

### 4.7 Start the bot and the clip worker

```sh
docker compose up -d
docker compose ps
```

After about a minute, `bot` should show **healthy** and `clip-worker` should be **running**.

### 4.8 Read the startup logs

```sh
docker compose logs --tail=50 bot
docker compose logs --tail=50 clip-worker
```

**The bot** should log, in order:
- `Connected to the database.`
- `HTTP API listening on port 3000`
- one `Loaded extension yks.bot.cogs.…` line per cog
- `Logged in as <bot name> (…) in N guild(s)`

If `MAIN_FEED_RSS` / `BONUS_FEED_RSS` are empty, it also logs a warning that the new-episode watcher is disabled.

**The clip worker** should log `Clip worker ready; queue: {...}`, showing how many clips are in each status. If any are `pending`, it continues with `Starting the transcription process.` and `Loaded Whisper model tiny.en …`; the first time, loading includes downloading the model into the `whisper-models` volume. After that come `Transcribing clip …` and `Clip … done` lines. With an empty queue it just waits; that's normal.

### 4.9 Register the slash command

In any channel, as the owner:

```
!sync
```

The bot replies `Synced 1 command(s) globally.` It can take a while for `/findtheclimp` to show up for everyone. To use it straight away in the current server, send `!sync here`.

### 4.10 Smoke-test everything

Use a member account that isn't the owner; the owner skips cooldowns, which can hide problems.

| Try | Expect |
| --- | --- |
| `!ping` (as the owner) | `Pong. Gateway … ms, database ok (… ms).` |
| `!help` | Help embed with the three categories |
| `!help other` | List of other commands, mentioning the clip channel |
| `!bongo` | `https://ghostbongo.bigcartel.com` |
| `garm` (just the word) | The bot replies `garm` (at most once per 3 hours per server) |
| `!mug` | One of the mug pictures |
| `!burgy` | `Burgy left N days ago.` |
| `!latest` | Two embeds: the newest main episode and the newest Patreon-only episode |
| `!today` | Episodes released on this date in past years, or `Nothing happened on this date.` |
| `!best` | `Top N Episodes`, matching the old bot's numbers |
| `!topmail` | Top mailbag messages from the last 30 days, or the "No mailbag messages…" line |
| `!topks` | Top kickstarters, if there are any |
| `!climp` | A random clip from the clip channel |
| `/findtheclimp` + a word from a known clip | Suggestions appear; picking one shows a private preview with **Post it** / **Don't post it** |
| Join a voice channel, `!listen play 1` | `Downloading episode...`, then the bot joins and a **Now playing** embed with ⏸ ⏹ ▶️ appears. Try ⏸, then ▶️, then `!listen stop`. |
| `!newmember <your user ID>` (as the owner) | Usually nothing: you've already been welcomed, and each member is only welcomed once per server. To see the welcome image, use the ID of a member who joined after the switch-over, or run it on a test server. |
| `!welcome` twice (as a mod) | `Welcome message disabled.` then `Welcome message enabled.` (leave it enabled) |

### 4.11 Re-point the im-at.work site

The site posts shared clips to `POST http(s)://<this server>:<PORT>/api/share-clip`, or through a reverse proxy if you use one. If the new bot runs on a different server or port than the old one, update the site's setting. Keep the same `BOT_AUTH_USERNAME` / `BOT_AUTH_PASSWORD`.

Test it from any machine:

```sh
curl -i http://<this server>:<PORT>/healthz
# {"ok": true, "discord": true}

curl -i -u "$BOT_AUTH_USERNAME:$BOT_AUTH_PASSWORD" \
  -H "Content-Type: application/json" \
  -d '{"userId":"<your user ID>","page":"test","url":"https://<a small public .mp4 URL>"}' \
  http://<this server>:<PORT>/api/share-clip
# {"ok": true}, and the clip appears in the clip channel
```

If the site was reaching the old bot over HTTPS, put the same reverse proxy (nginx, Caddy, …) in front of the new one. The bot itself serves plain HTTP.

### 4.12 Record clips posted while neither bot was online

As the owner:

```
!backfillclips
```

It walks the whole clip channel's history, records any audio or video the database doesn't have yet, and ends with `Clip backfill done: N new clip(s).` Clips already imported are skipped, so it's safe to run any time.

---

## Part 5 — After the switch-over

### 5.1 Let the clip worker catch up

Imported clips without a transcript, plus anything `!backfillclips` found, are queued. Follow the progress:

```sh
docker compose logs -f clip-worker
docker compose exec postgres psql -U yks -d yks -c "SELECT status, count(*) FROM clips GROUP BY status;"
```

With `tiny.en` on a typical CPU, a short clip takes a few seconds. New clips always go to the front of the queue. Once the queue is empty, the worker frees the model after `WHISPER_IDLE_UNLOAD_SECONDS`.

Clips marked `failed` have the reason in `last_error`. To retry them all:
```sh
docker compose exec postgres psql -U yks -d yks -c "UPDATE clips SET status='pending', attempts=0, retry_at=NULL WHERE status='failed';"
```

### 5.2 Watch the first day

```sh
docker compose ps        # all running; bot "healthy"
docker stats --no-stream # memory per container
docker compose logs --since 1h bot | grep -E "ERROR|WARNING"
```

When the next episode comes out, check that it's announced once, in the `!rssChannel` channel, with the "oh no they did another" reactions.

### 5.3 Set up backups

Back up Postgres daily. For example, add this to the server's crontab (`crontab -e`), adjusting the path:

```cron
15 4 * * * cd /path/to/pyks-smart-bot && docker compose exec -T postgres pg_dump -U yks -d yks --format=custom > backups/yks-$(date +\%F).dump && find backups -name 'yks-*.dump' -mtime +14 -delete
```

Create the `backups` folder first (`mkdir backups`). To restore a backup, with the bot and worker stopped:

```sh
docker compose stop bot clip-worker
docker compose exec -T postgres pg_restore -U yks -d yks --clean --if-exists < backups/yks-YYYY-MM-DD.dump
docker compose up -d
```

### 5.4 Update to a new version

```sh
git pull
docker compose up -d --build
```

Database migrations run automatically through the `migrate` service before the bot starts. After an update that changes slash commands, send `!sync` again.

### 5.5 Retire the old setup

Once you're happy, after a week or so:

1. **Keep a final copy of MongoDB**, just in case:
   ```sh
   mongodump --uri "$MONGO_URL" --archive=yks-mongo-final.archive
   ```
   Any machine with the MongoDB Database Tools installed can do this.
2. Remove `MONGO_URL` from `.env`, and remove the server's IP from the Atlas access list.
3. Shut down the TypeScript bot's server or container for good, and the MongoDB database if nothing else uses it.

---

## Part 6 — Rolling back

MongoDB is never modified by any of this, so going back to the TypeScript bot is just:

```sh
docker compose stop bot clip-worker
```

Then start the TypeScript bot again (reverse step 4.4), and re-point the im-at.work site if you changed it in 4.11.

Anything that happened while the new bot was running isn't in MongoDB: new `!best` votes, `:this:` tallies, welcomed members, new clips and their transcripts. If you switch over again later, repeat Part 4 from 4.4. Before the final import (4.5), **truncate the tables first**, so Postgres matches MongoDB exactly. Then run `!backfillclips` (4.12) to pick up clips posted in between.

---

## Part 7 — Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| The bot is online but ignores `!` commands | **Message Content Intent** is off (step 1.3). Turn it on, then `docker compose restart bot`. |
| No welcome messages | **Server Members Intent** is off, `!welcome` was toggled off, or the server has no system channel set (Server Settings → Overview → System Messages Channel). |
| Every command is answered twice | The TypeScript bot is still running (step 4.4). |
| `/findtheclimp` doesn't appear | Run `!sync`. Global registration can take a while; `!sync here` is instant for the current server. |
| `!sync` / `!ping` do nothing | `OWNER_ID` isn't your user ID (step 1.4). Fix it in `.env`, then `docker compose up -d bot`. |
| `bot` exits with `password authentication failed` | `POSTGRES_PASSWORD` was changed after the database was created (step 2.3). Either put the old value back, or change the real password: `docker compose exec postgres psql -U yks -d yks -c "ALTER USER yks PASSWORD 'new-password';"`, then `docker compose up -d`. |
| `migrate` fails | Read `docker compose logs migrate`. It's usually the same password problem, or Postgres not being healthy yet; run `docker compose up -d migrate` again. |
| `bind: address already in use` on port 3000 | Something else (often the old bot) is using `PORT`. Stop it, or set a different `PORT` in `.env`. |
| `bot` shows `unhealthy` | `docker compose logs bot`. The HTTP API didn't start, or the process is stuck. `curl localhost:$PORT/healthz` on the server shows whether it's reachable. |
| Import: `ServerSelectionTimeoutError` | The server can't reach MongoDB: check the Atlas IP access list (2.5), the host name, and the `mongodb+srv://` vs `mongodb://` form. |
| Import: `Authentication failed` | Wrong user or password in `MONGO_URL`, a special character not percent-encoded, or `authSource=admin` missing for self-hosted. |
| Import: `pymongo is needed` | You ran `yks-import-mongo` without `--with "pymongo>=4.10"`. Use the exact command from 3.3. |
| Import: `No default database name defined` | `MONGO_URL` has no `/DBNAME` part. Add it, or set `MONGO_DB` in `.env`. |
| The clip worker logs `AUTH_TOKEN is not set` | `.env` is missing the token, or isn't next to `docker-compose.yml`. |
| Many clips fail with `Discord API error … HTTP 403` | The bot can't read the clip channel. It needs **View Channel** and **Read Message History** there. |
| The clip worker can't download the model | The server can't reach `huggingface.co`. Allow it, at least for the first start; after that the model is cached in the `whisper-models` volume. |
| The new episode wasn't announced | No `!rssChannel` is set (check `SELECT * FROM guild_settings;`), the feed URLs are wrong, or the episode came out while the bot was offline. The watcher only announces changes it sees while running. |
| `!listen` says `Failed to join the voice channel` | The bot is missing Connect/Speak in that channel, or voice traffic is blocked. Voice uses UDP to Discord's voice servers, so the server must allow outbound UDP. |

To get help, include `docker compose ps -a` and the last 100 lines of `docker compose logs <service>`.
