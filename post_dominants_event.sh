ENV_FILE=xanadu_kink.env

cp -f "$ENV_FILE" .env
fetlife login
fetlife whoami
fetlife --env-file "$ENV_FILE" post-event \
  --event-id VDM111926 \
  --events-file ~/github-projects/Xanadu/events-data.js

