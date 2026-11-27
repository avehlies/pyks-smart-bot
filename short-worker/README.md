# YKS short worker

A sidecar to the YKS Smart Bot that watches the show's YouTube Shorts ([@YourKickstarterSucks/shorts](https://www.youtube.com/@YourKickstarterSucks/shorts)), and records them in the `youtube_shorts` table. The bot posts each new one in `#yks-shit`, and its `!short` command picks a random one from the table. It's a separate application with its own process and Docker image (the `pyks_short_worker` service): the bot never talks to YouTube, and the worker never talks to Discord. It imports no bot code; the two share only the database.

## How it works

Every `SCAN_INTERVAL_MINUTES` (default 30) the worker:

1. Reads two public sources and merges them:
   - the channel's Shorts-only RSS feed (`/feeds/videos.xml?playlist_id=UUSH…`), with the newest 15 shorts, their titles and publish dates;
   - the channel's Shorts page, with the newest ~48 shorts and their titles, read from the JSON embedded in the page.

   If one source fails (YouTube changes its page layout now and then), the other still works, and the failure is logged as a warning. Only the first screen of the Shorts tab is read, so on the first run the table gets the newest ~48 shorts, not the whole back catalogue.
2. Inserts shorts it hasn't seen before, and marks the ones that must never be posted by setting `announced_at`, in the same transaction:
   - On the **first scan** (an empty table), everything it finds. **Nothing is posted on the first deploy**: only shorts uploaded after it are.
   - Shorts the RSS feed says were published more than `ANNOUNCE_MAX_AGE_HOURS` ago.
   - Shorts seen only on the Shorts page that the feed hasn't dated within `ANNOUNCE_MAX_AGE_HOURS` of first seeing them. Without a date, a new short can't be told apart from an old one the first scan missed (for example, if the page was down then and the feed only lists the newest 15). New uploads always show up in the feed, so they get a date within a scan or two.

## Posting (in the bot)

Every minute, the bot (`src/yks/bot/cogs/shorts.py`) posts shorts that have a publish date and no `announced_at`, oldest first and at most 5 at a time, in `YKS_SHORTS_CHANNEL_ID` as `New short! https://www.youtube.com/shorts/<id>`. Discord unfurls the title and thumbnail. The message id goes in `announced_message_id`.

A short is never posted twice. The bot claims each one by setting `announced_at`, in its own committed transaction, *before* posting it, so a second poll or a second bot process can't take the same short. What happens when a post fails:

- **Discord refuses it** (a 4xx, such as missing permissions): nothing was posted, so the claim is released and the next poll retries.
- **Discord returns a 5xx, the request times out, or the bot dies mid-post:** the message may have gone out, so the short stays claimed and isn't retried. At worst a short is missed, never doubled. The log says which short.

If `YKS_SHORTS_CHANNEL_ID` isn't set, the bot doesn't post, and shorts wait in the table.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://yks:yks@localhost:5432/yks` | Same database as the bot. `postgresql+asyncpg://` URLs are accepted. |
| `YOUTUBE_HANDLE` | `YourKickstarterSucks` | The channel's @handle, for the Shorts page. |
| `YOUTUBE_CHANNEL_ID` | `UCt_yVzAjE5AQUNMlLAw5jtA` | The channel's `UC…` id, for the RSS feed. |
| `SCAN_INTERVAL_MINUTES` | `30` | |
| `ANNOUNCE_MAX_AGE_HOURS` | `72` | Older shorts, or ones the feed doesn't date within this long, are marked as not to be posted. |

Posting is configured on the bot: `YKS_SHORTS_CHANNEL_ID` is the channel (and the only one `!short` answers in). The bot needs **Send Messages** and **Embed Links** there.
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
-- have the bot post a short again within a minute (the worker re-marks it on its next scan if it's
-- older than ANNOUNCE_MAX_AGE_HOURS):
UPDATE youtube_shorts SET announced_at = NULL, announced_message_id = NULL WHERE video_id = '...';
```
