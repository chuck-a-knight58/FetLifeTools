cd ~/Documents/github-projects/FetLifeTools
source .venv/bin/activate
fetlife events-calendar --out ~/Documents/github-projects/Xanadu/XR/community_calendar.html
cd ~/Documents/github-projects/Xanadu
git add .
git commit -m"Community calendar update"
git push