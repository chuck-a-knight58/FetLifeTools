cd ~/Documents/github-projects/FetLifeTools
source .venv/bin/activate

# Fetch the events only if community_calendar.js wasn't already updated today.
if [ ! -f community_calendar.js ] || [ "$(stat -f %Sm -t %Y-%m-%d community_calendar.js)" != "$(date +%Y-%m-%d)" ]; then
    fetlife events-calendar --out community_calendar.js
else
    echo "community_calendar.js is already from today; skipping events-calendar."
fi

# Copy each file only if it's newer than the site's copy (or the site has none).
for f in community_calendar.html community_calendar.js; do
    if [ ! -e ~/Documents/github-projects/Xanadu/XR/"$f" ] || [ "$f" -nt ~/Documents/github-projects/Xanadu/XR/"$f" ]; then
        cp "$f" ~/Documents/github-projects/Xanadu/XR/"$f"
    fi
done

cd ~/Documents/github-projects/Xanadu
git add .
git commit -m"Community calendar update"
git push
