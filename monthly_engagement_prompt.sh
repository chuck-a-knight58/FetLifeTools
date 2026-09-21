#!/bin/bash -x

# This script sends friend requests to engagers who are not currently friends or followers of a specified FetLife user.

# 1) Use the credentials found in .env
cp xanadu_kink.env .env
fetlive login

# 2) List all of Xanadu_Kink’s friends and followers and write the output to the file friends.csv
fetlife engagement Xanadu_Kink

# 3) Use the file friends.csv to find people that have been engaged but are not currently friends or followers
fetlife engagement Xanadu_Kink --since “1 month” --connections friends.csv --csv > strangers.csv

# 4) Use the file strangers.csv to send a friend request to those engagers
fetlife friend-requests strangers.csv

# 5) Use the file strangers.csv to send a welcome message to those engagers
fetlife message --from-csv strangers.csv --subject "A Warm Welcome From Xanadu Kink" --body-file greeting.txt --yes
