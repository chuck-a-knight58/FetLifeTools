#!/bin/bash

cd ~/github-projects/FetLifeTools
source .venv/bin/activate

# This script sends friend requests to engagers who are not currently friends or followers of a specified FetLife user.

# 1) Log in as Xanadu_Kink (every command below uses xanadu_kink.env, not .env)
fetlife --env-file xanadu_kink.env login

# 2) List all of Xanadu_Kink’s friends and followers and write the output to the file friends.csv
fetlife --env-file xanadu_kink.env connections Xanadu_Kink --out friends.csv

# 3) Use the file friends.csv to find people that have been engaged but are not currently friends or followers
fetlife --env-file xanadu_kink.env engagement Xanadu_Kink --since "1 month" --connections friends.csv --csv > strangers.csv

# 4) Use the file strangers.csv to send a friend request to those engagers
fetlife --env-file xanadu_kink.env friend-requests strangers.csv

# 5) Use the file strangers.csv to send a welcome message to those engagers
fetlife --env-file xanadu_kink.env message --from-csv strangers.csv --subject "A Warm Welcome From Xanadu Kink" --body-file assets/greeting.txt --yes
