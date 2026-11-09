#!/usr/bin/env bash
# Re-transcribe existing clips with the clip worker's current model, one clip at a time.
#
# /findtheclimp only searches clips with status 'done', so requeueing everything at once would
# empty the search until the worker caught up. This puts one clip in the queue, waits for the queue
# to empty, then does the next, so only the clip being worked on is missing from search. New posts
# are handled first, since the queue has to be empty before the next old clip goes in.
#
# Run it on the Docker host, after the worker has been recreated with the new model. Ctrl-C is safe:
# run it again with the same --before and it carries on where it stopped.
#
# A clip that can't be re-transcribed keeps its old transcript and goes back to 'done' with
# last_error set. Those clips aren't tried again, and the run's summary counts them.

set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-pyks_postgres}"
PG_USER="${PG_USER:-yks}"
PG_DB="${PG_DB:-yks}"
WORKER_CONTAINER="${WORKER_CONTAINER:-pyks_clip_worker}"
POLL_SECONDS="${POLL_SECONDS:-2}"

usage() {
    cat <<EOF
Usage: $(basename "$0") --model NAME --before TIMESTAMP [--dry-run]

  --model NAME        The model the worker must be running, e.g. small.en. A safety check.
  --before TIMESTAMP  Redo clips last transcribed before this time, and clips with no recorded
                      transcription time (imported clips). Use the moment the new model went live,
                      with a UTC offset, and keep the same value if you restart the script.
  --dry-run           Show what would be done and change nothing.

Environment: PG_CONTAINER, PG_USER, PG_DB, WORKER_CONTAINER, POLL_SECONDS.
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

MODEL=""
BEFORE=""
DRY_RUN=0
while (($#)); do
    case "$1" in
        --model) MODEL="${2:-}"; shift 2 || die "--model needs a value" ;;
        --before) BEFORE="${2:-}"; shift 2 || die "--before needs a value" ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
done
[[ -n "$MODEL" && -n "$BEFORE" ]] || { usage >&2; die "--model and --before are required"; }

# Runs the SQL on stdin. :'before' in the SQL is the --before value, safely quoted by psql.
sql() {
    docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" \
        -qAt -v ON_ERROR_STOP=1 -v before="$BEFORE"
}

# Old transcripts only. `last_error IS NULL` leaves out clips this script already failed to redo.
OLD_TRANSCRIPT="NOT deleted AND (transcribed_at IS NULL OR transcribed_at < :'before'::timestamptz)"

count_remaining() {
    sql <<SQL
SELECT count(*) FROM clips WHERE status = 'done' AND last_error IS NULL AND $OLD_TRANSCRIPT;
SQL
}

count_queued() {
    sql <<SQL
SELECT count(*) FROM clips WHERE status IN ('pending', 'processing') AND NOT deleted;
SQL
}

# A clip that failed for good would otherwise stay out of search despite having a usable transcript.
restore_failed() {
    sql <<SQL >/dev/null
UPDATE clips SET status = 'done'
WHERE status = 'failed' AND transcription IS NOT NULL AND $OLD_TRANSCRIPT;
SQL
}

# Requeues the newest remaining clip and prints its id, or nothing when none are left. The trigger
# only notifies on INSERT, so without pg_notify each clip would wait for the worker's next poll.
requeue_next() {
    sql <<SQL
WITH next AS (
    SELECT id FROM clips
    WHERE status = 'done' AND last_error IS NULL AND $OLD_TRANSCRIPT
    ORDER BY id DESC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
), requeued AS (
    UPDATE clips c
    SET status = 'pending', attempts = 0, retry_at = NULL, claimed_at = NULL
    FROM next WHERE c.id = next.id
    RETURNING c.id
)
SELECT id FROM requeued CROSS JOIN LATERAL pg_notify('clips_new', id::text);
SQL
}

wait_for_empty_queue() {
    local noted=$SECONDS queued
    while queued="$(count_queued)" && ((queued > 0)); do
        # Failed clips wait up to 9 minutes to retry, so a pause here isn't necessarily a problem.
        if ((SECONDS - noted >= 300)); then
            noted=$SECONDS
            echo "Still waiting on $queued queued clip(s). Is $WORKER_CONTAINER running?"
        fi
        sleep "$POLL_SECONDS"
    done
}

summary() {
    sql <<SQL
SELECT 'Kept old transcript after a failed redo: ' || count(*) FROM clips
WHERE status = 'done' AND last_error IS NOT NULL AND $OLD_TRANSCRIPT;
SQL
}

running_model="$(docker exec "$WORKER_CONTAINER" printenv WHISPER_MODEL || true)"
[[ "$running_model" == "$MODEL" ]] ||
    die "$WORKER_CONTAINER is running model '${running_model:-unset}', not '$MODEL'. Recreate it first."

in_past="$(sql <<'SQL'
SELECT :'before'::timestamptz <= now();
SQL
)"
[[ "$in_past" == "t" ]] || die "--before is in the future: $BEFORE"

total="$(count_remaining)"
echo "Worker model: $running_model"
echo "Cutoff:       $BEFORE"
echo "Clips to redo: $total"
if ((DRY_RUN)); then
    echo "Dry run: nothing changed."
    exit 0
fi

trap 'echo; echo "Stopped. Any clip already queued will still finish. Re-run with the same --before to continue."; exit 130' INT

done_count=0
started=$SECONDS
while true; do
    wait_for_empty_queue
    restore_failed
    id="$(requeue_next)"
    [[ -n "$id" ]] || break
    done_count=$((done_count + 1))
    elapsed=$((SECONDS - started))
    left=$(((total - done_count) * elapsed / done_count))
    printf '[%d/%d] requeued clip %s (about %dh %02dm left)\n' \
        "$done_count" "$total" "$id" $((left / 3600)) $((left % 3600 / 60))
done

echo "Done: requeued $done_count clip(s)."
summary
