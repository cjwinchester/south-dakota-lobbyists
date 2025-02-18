# South Dakota lobbyist data

_Updated February 17, 2025_

tl;dr: Building a more complete dataset of public and private lobbyists in South Dakota.

- [The problem](#The-problem)
- [The solution](#The-solution)
- [The results](#The-results)

---

### The problem
You can download (some) data on South Dakota lobbyists via [the state's website](https://sosenterprise.sd.gov/BusinessServices/Lobbyist/LobbyistSearch.aspx), but ...
- For **private lobbyists**, you can only export data for recent years: The select menu that allows you to filter for a year of data only goes back to 2021, while the dataset includes records going back to 2012. And each exported file -- a zip archive containing a single pipe-delimited text file -- has incomplete data compared with the data returned via search or by reviewing the registration detail page.
- For **public lobbyists**, there is no data export.

The goal here is to scrape the most detailed data with the maximum date range for each type of lobbyist, ignoring the data exports and instead pulling data from the results table, then from each registration detail page.

You can't return every lobbyist record with a blank search, however, because results are limited to 1,000 records. And because the select menu filter doesn't cover every year in the data, you can't cycle through the options to scrape all records for each year.

### The solution

[Here's the Python script](download.py).

A blank search only returns 1,000 records in the results table, but if you click the `Printer Friendly Version` button, the site generates a PDF containing _all_ search results, not just the first 1,000. The data in the PDF is pretty basic, but it does include the _name_ of each lobbyist, which is one way you can filter results on the search page.

So if you extract the names of the private lobbyists from the PDF and parse them into their constituent parts (given name, surname, etc.), you can turn around and plug each name back into the search page, scrape the detailed data from the search results table, then download and scrape each detail page in turn. Bingpot!

That's the workflow I settled on, using:
- [`playwright`](https://playwright.dev/python/) to manage the browser automation
- [`pdfplumber`](https://github.com/jsvine/pdfplumber) to extract data from the PDFs
- [`probablepeople`](https://github.com/datamade/probablepeople) to parse names (results cached in [`private/parsed_names.json`](private/parsed_names.json))
- [`usaddress-scourgify`](https://github.com/GreenBuildingRegistry/usaddress-scourgify) to parse addresses

For the private lobbyists, the final step is to check the scraped data against the data extracted from the PDF to make sure nothing is missing.

### The results

#### [`private/south-dakota-lobbyists-private.json`](private/south-dakota-lobbyists-private.json)
- Each record is a _lobbyist registration_ for one legislative session, meaning the same lobbyist could appear more than once if they lobbied for multiple legislative sessions
- Record count: **9,219** registration records, including 1,451 that don't reference any financial disclosure forms. The rest of them collectively point to 17,679 disclosure forms
- Date range: 2012-01-03 to 2025-02-17
- Record layout:
    - `url`: Lobbyist registration detail page URL
    - `year`: Registration year
    - `registration_number`: Unique identifier for this registration record
    - `lobbyist_name`: Attempted parse with [probablepeople](https://github.com/datamade/probablepeople), with results cached in [`private/parsed-names.json`](private/parsed-names.json)
        - `lobbyist_name.name_full` (always present)
        - `lobbyist_name.name_first`
        - `lobbyist_name.name_middle`
        - `lobbyist_name.name_last`
    - `lobbyist_status`: "ACTIVE" or "UNREGISTERED"
    - `lobbyist_employment_date`
    - `lobbyist_phone`
    - `lobbyist_email`
    - `lobbyist_address`: Attempted parse with [`usaddress-scourgify`](https://github.com/GreenBuildingRegistry/usaddress-scourgify)
        - `lobbyist_address.address_full` (always present)
        - `lobbyist_address.address_line_1`
        - `lobbyist_address.address_line_2`
        - `lobbyist_address.city`
        - `lobbyist_address.state`
        - `lobbyist_address.postal_code`
    - `lobbyist_occupation`
    - `lobbyist_type`: "PRIVATE"
    - `employer_name`
    - `employer_agent_name`
    - `employer_registration_date`
    - `employer_authorization_date`
    - `employer_lobbying_subjects`
    - `employer_registration_status`: "ACTIVE", "UNREGISTERED", "WITHDRAWN" or "PENDING AUTHORIZATION"
    - `employer_address`: Attempted parse with [`usaddress-scourgify`](https://github.com/GreenBuildingRegistry/usaddress-scourgify)
        - `employer_address.address_full` (always present)
        - `employer_address.address_line_1`
        - `employer_address.address_line_2`
        - `employer_address.city`
        - `employer_address.state`
        - `employer_address.postal_code`
    - `filings`: A list of filings attached to this registration record. Each filing includes:
        - `filing_type`: "Lobbyist Expense Report", "Employer Expense Report" or "Withdrawal"
        - `filing_date`
        - `filing_number`: Filing ID
        - `filing_url`: PDF link
        - `filing_guid`: Unique identifier, taken from the `id` parameter in `filing_url`

#### [`public/south-dakota-lobbyists-public.csv`](public/south-dakota-lobbyists-public.csv)
- Record count: 5,085
- Date range: 2012 to 2025
- Record layout:
    - `year`
    - `lobbyist_name`
    - `agency`
    - `agency_address`