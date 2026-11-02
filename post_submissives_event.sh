ENV_FILE=xanadu_sub_group.env

cp -f "$ENV_FILE" .env
fetlife login
fetlife whoami
fetlife --env-file "$ENV_FILE" post-event \
  --event-id VSM021027 \
  --events-file ~/github-projects/Xanadu/events-data.js

