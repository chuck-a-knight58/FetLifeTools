export ASSETS_PATH=~/github-projects/FetLifeTools:~/github-projects/FetLifeTools/assets:~/github-projects/Xanadu:~/github-projects/Xanadu/assets

ENV_FILE=xanadu_kink.env

cp -f "$ENV_FILE" .env
fetlife login
fetlife whoami
fetlife --env-file "$ENV_FILE" post-event \
  --draft \
  --event-id VDM012827 \
  --events-file ~/github-projects/Xanadu/events-data.js

