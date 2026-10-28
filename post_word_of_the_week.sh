#!/bin/bash
set -euo pipefail

cd ~/github-projects/FetLifeTools
source .venv/bin/activate

# This script publishes a new Word of the Week post automatically from a text file.
# Entries in TOPICS are separated by a line of five = signs. Each run posts the top
# entry (its first line is the title, the rest the body) and, once the post
# succeeds, removes that entry and its separator from the file.

ENV_FILE=xanadu_kink.env
TOPICS=assets/WOTW.txt

# The top entry runs up to the first ===== line (or to the end of the file).
end=$(awk '/^=====[[:space:]]*$/ { print NR; exit }' "$TOPICS")
if [ -z "$end" ]; then
    end=$(( $(wc -l < "$TOPICS") + 1 ))
fi
entry=$(awk -v e="$end" 'NR < e' "$TOPICS")

# TITLE: the entry's first non-blank line. BODY: every line after it, newlines kept as they are.
TITLE=$(printf '%s\n' "$entry" | grep -m 1 -v '^[[:space:]]*$' || true)
BODY=$(printf '%s\n' "$entry" | awk 'seen { print } !seen && NF { seen = 1 }')
if [ -z "$TITLE" ]; then
    echo "No entries left in $TOPICS; nothing to post." >&2
    exit 1
fi
if [ -z "$BODY" ]; then
    echo "The top entry of $TOPICS ($TITLE) has no body; fix it and rerun." >&2
    exit 1
fi

# 1) Log in (every command below uses $ENV_FILE, not .env)
fetlife --env-file "$ENV_FILE" login

# 2) Post it. With `set -e`, a failed post stops the script here and the entry stays in the file.
fetlife --env-file "$ENV_FILE" post-writing \
--title "$TITLE" \
--body "$BODY" \
--tag Wednesday \
--category Note \
--collections "The Xanadu Word" \
--image-consent \
--privacy public \
--yes

# 3) Posted: remove the entry and its separator from the file.
awk -v e="$end" 'NR > e' "$TOPICS" > "$TOPICS.tmp" && mv "$TOPICS.tmp" "$TOPICS"
echo "Posted and removed from $TOPICS: $TITLE"
