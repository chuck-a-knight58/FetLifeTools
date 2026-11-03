ENV_FILE=xanadu_kink.env

cp -f "$ENV_FILE" .env
fetlife login
fetlife whoami
fetlife --env-file "$ENV_FILE" post-event \
  --event-id ETX120526 \
  --events-file ~/github-projects/Xanadu/events-data.js

