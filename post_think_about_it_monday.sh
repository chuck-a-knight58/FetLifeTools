#!/bin/bash
set -euo pipefail

cd ~/github-projects/FetLifeTools
source .venv/bin/activate

# This script publishes a new think about it Monday post automatically using a text file and the signature image.
# Each run posts the top entry of TOPICS and, once the post succeeds, removes that entry from the file.

ENV_FILE=xanadu_kink.env
TOPICS=assets/TAIM.txt

# The top entry (the first non-blank line) and its line number.
entry=$(grep -n -m 1 -v '^[[:space:]]*$' "$TOPICS" || true)
if [ -z "$entry" ]; then
    echo "No entries left in $TOPICS; nothing to post." >&2
    exit 1
fi
line=${entry%%:*}
BODY=${entry#*:}

# 1) Log in (every command below uses $ENV_FILE, not .env)
fetlife --env-file "$ENV_FILE" login

# 2) Post it. With `set -e`, a failed post stops the script here and the entry stays in the file.
fetlife --env-file "$ENV_FILE" post-writing \
--title "Think About it Monday" \
--body "$BODY" \
--image assets/TAIM.png \
--tag Monday \
--category Note \
--collections "Monday" \
--image-consent \
--privacy public \
--yes

# 3) Posted: remove that entry from the file.
awk -v n="$line" 'NR != n' "$TOPICS" > "$TOPICS.tmp" && mv "$TOPICS.tmp" "$TOPICS"
echo "Posted and removed from $TOPICS: $BODY"
