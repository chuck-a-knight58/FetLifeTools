#!/usr/bin/env bash
#
# Run the discover crawl to completion, waiting out FetLife's rate limit.
#
# `fetlife discover` exits 75 (EX_TEMPFAIL) when FetLife is throttling us and the
# crawl still has a frontier left. That is the only status worth retrying: exit 0
# means the search finished, and anything else is a real error that sleeping
# won't fix.
#
# The first attempt starts a fresh search (CENTER/RADIUS/UNITS); every attempt
# after it resumes, because the crawl writes its frontier to STATE as it goes.
# The script decides which by looking for STATE, so it bootstraps a new crawl
# and continues an existing one with the same invocation — rerun it after an
# interruption and it picks up where it left off.
#
# Tunables (environment):
#   CENTER         circle center for a fresh crawl        (default Washington, NJ)
#   RADIUS         circle radius for a fresh crawl        (default 100)
#   UNITS          mi or km                               (default mi)
#   MAX_PAGES      friends/followers pages per profile    (default 1)
#   MAX_VISITS     profiles to crawl across the search    (default 12000)
#   STATE          resumable frontier + visited set       (default ~/.fetlife/discover_state.json)
#   RETRY_HOURS    whole hours to sleep after a throttle  (default 4)
#   RETRY_SECONDS  overrides RETRY_HOURS, for finer waits (default RETRY_HOURS*3600)
#   MAX_ATTEMPTS   throttle retries before giving up      (default 50)
#   OUT            file the crawl output is appended to   (default discover.txt)
#
# MAX_VISITS is a budget for the entire search, counted across resumes, so it
# has to cover the whole target — `discover`'s own default of 300 is a safety
# valve for ad-hoc runs, not a useful ceiling for a multi-day crawl. MAX_ATTEMPTS
# is purely the throttle-retry budget: the loop only repeats on exit 75, so a
# long crawl needs plenty of them.
#
# MAX_PAGES is deliberately 1. Each extra page costs a request per list, which
# roughly doubles the requests per profile and, on a crawl this size, buys more
# throttling than reach.
#
# Keep RETRY_HOURS at or above `discover --cooldown` (default 3h), or the next
# attempt is refused by the cooldown guard before it sends a single request.

set -uo pipefail

CENTER="${CENTER:-Washington, NJ}"
RADIUS="${RADIUS:-100}"
UNITS="${UNITS:-mi}"
MAX_PAGES="${MAX_PAGES:-1}"
MAX_VISITS="${MAX_VISITS:-12000}"
STATE="${STATE:-$HOME/.fetlife/discover_state.json}"
RETRY_HOURS="${RETRY_HOURS:-4}"
RETRY_SECONDS="${RETRY_SECONDS:-$((RETRY_HOURS * 3600))}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-50}"
OUT="${OUT:-discover.txt}"

EXIT_RATE_LIMITED=75

log() { printf '[go.sh %s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    log "attempt ${attempt}/${MAX_ATTEMPTS}"

    # Resume whenever there is state to resume; otherwise seed a new search.
    # A fresh attempt that is throttled before it can save leaves no state, so
    # the next attempt correctly starts fresh again rather than erroring out.
    if [[ -f "$STATE" ]]; then
        args=(--resume)
    else
        log "no state at ${STATE}; starting a fresh crawl of ${RADIUS}${UNITS} around '${CENTER}'."
        args=(--center "$CENTER" --radius "$RADIUS" --units "$UNITS")
    fi

    # Login is cheap (cached cookies) but can itself be throttled, so it shares
    # the retry path rather than aborting the run.
    fetlife login
    status=$?
    if ((status == 0)); then
        fetlife discover "${args[@]}" --state "$STATE" \
            --max-visits "$MAX_VISITS" --max-pages "$MAX_PAGES" >>"$OUT" 2>&1
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
