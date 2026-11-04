export ASSETS_PATH=~/github-projects/FetLifeTools:~/github-projects/FetLifeTools/assets:~/github-projects/Xanadu:~/github-projects/Xanadu/assets

ENV_FILE=xanadu_kink.env

cp -f "$ENV_FILE" .env
fetlife login
fetlife whoami

# See who would be invited; nothing is sent.
fetlife --env-file "$ENV_FILE" invite-to-event --DryRun \
  https://fetlife.com/events/2026/11/21/escape-to-xanadu-thanksgiving-play-party-gqkqfi \
  https://fetlife.com/groups/317599

