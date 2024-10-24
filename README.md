# South Dakota lobbyist data

_Updated October 23, 2024_

You can download data on South Dakota lobbyist registrations since 2021 using [this state website](https://sosenterprise.sd.gov/BusinessServices/Lobbyist/LobbyistSearch.aspx), but a) you can't export all the data at once, b) the download for each year is a zipfile containing a single pipe-delimited text file, some with records that include unescaped newlines, and c) you can't export the public lobbyist information as data.

This project [uses `playwright`](download.py) to navigate the form and collect lobbyist registration data in two tidy files:
- [`south-dakota-lobbyists-private.csv`](south-dakota-lobbyists-private.csv) (2,903 records)
- [`south-dakota-lobbyists-public.csv`](south-dakota-lobbyists-public.csv) (1,564 records)
    