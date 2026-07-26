#!/usr/bin/env bash
#
# Resume the discover crawl, waiting out FetLife's rate limit between attempts.
#
# `fetlife discover` exits 75 (EX_TEMPFAIL) when FetLife is throttling us and the
# crawl still has a frontier left. That is the only status worth retrying: exit 0
# means the search finished, and anything else is a real error that sleeping
# won't fix.
#
# Tunables (environment):
#   MAX_VISITS     profiles to crawl across the whole search (default 12000)
#   RETRY_HOURS    whole hours to sleep after a throttle   (default 4)
#   RETRY_SECONDS  overrides RETRY_HOURS, for finer waits  (default RETRY_HOURS*3600)
#   MAX_ATTEMPTS   throttle retries before giving up       (default 50)
#   OUT            file the crawl output is appended to    (default discover.txt)
#
# MAX_VISITS is a budget for the entire search, counted across resumes, so it
# has to cover the whole target — `discover`'s own default of 300 is a safety
# valve for ad-hoc runs, not a useful ceiling for a multi-day crawl. MAX_ATTEMPTS
# is purely the throttle-retry budget: the loop only repeats on exit 75, so a
# long crawl needs plenty of them.
#
# Keep RETRY_HOURS at or above `discover --cooldown` (default 3h), or the next
# attempt is refused by the cooldown guard before it sends a single request.

set -uo pipefail

MAX_VISITS="${MAX_VISITS:-12000}"
RETRY_HOURS="${RETRY_HOURS:-4}"
RETRY_SECONDS="${RETRY_SECONDS:-$((RETRY_HOURS * 3600))}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-50}"
OUT="${OUT:-discover.txt}"

EXIT_RATE_LIMITED=75

log() { printf '[go.sh %s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    log "attempt ${attempt}/${MAX_ATTEMPTS}"

    # Login is cheap (cached cookies) but can itself be throttled, so it shares
    # the retry path rather than aborting the run.
    fetlife login
    status=$?
    if ((status == 0)); then
        fetlife discover --resume --max-visits "$MAX_VISITS" >>"$OUT" 2>&1
        status=$?
    elif ((status != EXIT_RATE_LIMITED)); then
        log "login failed (exit ${status}); not retrying."
        exit "$status"
    fi

    case "$status" in
        0)
            log "crawl finished; results in ${OUT}"
            exit 0
            ;;
        "$EXIT_RATE_LIMITED")
            if ((attempt == MAX_ATTEMPTS)); then
                log "still rate-limited after ${MAX_ATTEMPTS} attempts; giving up."
                log "state is intact — rerun this script whenever you like."
                exit "$EXIT_RATE_LIMITED"
            fi
            log "rate-limited; sleeping ${RETRY_SECONDS}s before the next attempt."
            sleep "$RETRY_SECONDS"
            ;;
        *)
            log "discover failed (exit ${status}); not retrying."
            exit "$status"
            ;;
    esac
done
