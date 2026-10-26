#!/bin/bash

cd ~/github-projects/FetLifeTools
source .venv/bin/activate

# Run using anonymous credentials
fetlife --env-file hammerbacher.env login

# Fetch the events only if community_calendar.js wasn't already updated today.
if [ ! -f community_calendar.js ] || [ "$(stat -f %Sm -t %Y-%m-%d community_calendar.js)" != "$(date +%Y-%m-%d)" ]; then
    fetlife --env-file hammerbacher.env events-calendar --out community_calendar.js
else
    echo "community_calendar.js is already from today; skipping events-calendar."
fi

# Copy each file only if it's newer than the site's copy (or the site has none).
for f in community_calendar.js; do
    if [ ! -e ~/github-projects/Xanadu/"$f" ] || [ "$f" -nt ~/github-projects/Xanadu/"$f" ]; then
        cp "$f" ~/github-projects/Xanadu/"$f"
    fi
done

cd ~/github-projects/Xanadu
git add community_calendar.js
git commit -m"Community calendar update"
git push
