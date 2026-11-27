# YKS short worker

A sidecar to the YKS Smart Bot that watches the show's YouTube Shorts ([@YourKickstarterSucks/shorts](https://www.youtube.com/@YourKickstarterSucks/shorts)), records them in the `youtube_shorts` table, and posts each new one in `#yks-shit`. The bot's `!short` command picks a random one from that table. It's a separate application with its own process and Docker image (the `pyks_short_worker` service), so the bot itself never talks to YouTube. It imports no bot code; the two share only the database.

## How it works

Every `SCAN_INTERVAL_MINUTES` (default 30) the worker:

1. Reads two public sources and merges them:
   - the channel's Shorts-only RSS feed (`/feeds/videos.xml?playlist_id=UUSH…`), with the newest 15 shorts, their titles and publish dates;
   - the channel's Shorts page, with the newest ~48 shorts and their titles, read from the JSON embedded in the page.

   If one source fails (YouTube changes its page layout now and then), the other still works, and the failure is logged as a warning. Only the first screen of the Shorts tab is read, so on the first run the table gets the newest ~48 shorts, not the whole back catalogue.
2. Inserts shorts it hasn't seen before. On the **first scan** (an empty table) everything is marked as already announced, so **nothing is posted on the first deploy**: only shorts uploaded after it are.
3. Posts each new short in `YKS_SHORTS_CHANNEL_ID` as `New short! https://www.youtube.com/shorts/<id>` (Discord unfurls the title and thumbnail), oldest first, and records the message id in `announced_message_id`.

Only shorts the RSS feed has dated are posted, and only if they were published within `ANNOUNCE_MAX_AGE_HOURS`. A short seen only on the Shorts page waits for the feed to date it, and is recorded quietly if that doesn't happen within `ANNOUNCE_MAX_AGE_HOURS`. Without a date, a new short can't be told apart from an old one the first scan missed (for example, if the page was down then and the feed only lists the newest 15). New uploads always show up in the feed, so they get a date within a scan or two. If a post fails (missing permissions, Discord down), the short stays pending and is retried on the next scan. If `YKS_SHORTS_CHANNEL_ID` isn't set, shorts are recorded but not posted.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://yks:yks@localhost:5432/yks` | Same database as the bot. `postgresql+asyncpg://` URLs are accepted. |
| `AUTH_TOKEN` | (required) | The bot's token, used to post as the bot. The bot needs **Send Messages** and **Embed Links** in the channel. |
| `YKS_SHORTS_CHANNEL_ID` | (unset) | Where new shorts are posted. The bot's `!short` only answers in this channel. |
| `YOUTUBE_HANDLE` | `YourKickstarterSucks` | The channel's @handle, for the Shorts page. |
| `YOUTUBE_CHANNEL_ID` | `UCt_yVzAjE5AQUNMlLAw5jtA` | The channel's `UC…` id, for the RSS feed. |
| `SCAN_INTERVAL_MINUTES` | `30` | |
| `ANNOUNCE_MAX_AGE_HOURS` | `72` | Older shorts, or ones the feed doesn't date within this long, are recorded without being posted. |
| `HEARTBEAT_FILE` | `/tmp/yks-short-worker.heartbeat` | See [Health](#health). |
| `HEARTBEAT_INTERVAL_SECONDS` | `60` | |
| `HEARTBEAT_MAX_AGE_SECONDS` | `300` | |

## Health

The image has a Docker `HEALTHCHECK` (`yks-short-worker-health`). The worker touches `HEARTBEAT_FILE` every `HEARTBEAT_INTERVAL_SECONDS` while the database answers. YouTube being unreachable doesn't make it unhealthy; check the logs for `Couldn't read the RSS feed` / `Couldn't read the Shorts page` warnings.

## Running

With Docker, it's the `pyks_short_worker` service in the repository's `docker-compose.yml`.

Locally, from the repository root:

```sh
uv sync --all-packages
uv run yks-short-worker
```

## Checking on it

```sql
SELECT count(*), count(announced_message_id) AS posted FROM youtube_shorts;
SELECT video_id, title, published_at, announced_at FROM youtube_shorts
ORDER BY coalesce(published_at, first_seen_at) DESC LIMIT 10;
-- post a short again on the next scan (if it's within ANNOUNCE_MAX_AGE_HOURS):
UPDATE youtube_shorts SET announced_at = NULL, announced_message_id = NULL WHERE video_id = '...';
```
