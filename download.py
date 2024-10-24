from pathlib import Path
from datetime import date
import time
import zipfile
import io
import csv

from playwright.sync_api import sync_playwright


start_url = 'https://sosenterprise.sd.gov/BusinessServices/Lobbyist/LobbyistSearch.aspx'

dir_private = Path('private')
filepath_csv_private = 'south-dakota-lobbyists-private.csv'
filepath_csv_public = 'south-dakota-lobbyists-public.csv'

TODAY = date.today()
THIS_YEAR = TODAY.year


def download():

    select_id = 'ctl00_MainContent_slctYears'

    headers_public = [
        'year',
        'name_last',
        'name_first',
        'state_agency_or_tribe'
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(start_url)

        # private lobbyists
        select = page.locator(f'select#{select_id}')
        options = select.locator('option').all()
        button = page.locator('a#ctl00_MainContent_ExportButton')

        for opt in options:
            label = opt.inner_text()

            if label.lower() == 'all years':
                continue

            filename_zip = dir_private / f'{label}.zip'
            filename_txt = dir_private / f'{label}.txt'

            if filename_txt.exists() and int(label) < THIS_YEAR:
                continue

            select.select_option(label=label)

            with page.expect_download() as download_info:
                button.click()

            download = download_info.value
            download.save_as(filename_zip)

            print(f'Downloaded {str(filename_zip)}')
            time.sleep(2)

            # unzip
            with zipfile.ZipFile(filename_zip.resolve()) as zf:
                txt = zf.namelist()[0]
                with io.TextIOWrapper(zf.open(txt), encoding='utf-8') as infile, open(filename_txt, 'w') as outfile:
                    outfile.write(infile.read())

                    print(f'Unzipped to {filename_txt}')

            # delete zipfile
            filename_zip.unlink()

        # now grab public lobbyist data
        page.locator('#ctl00_MainContent_chkSearchByPublic').check()

        time.sleep(2)

        select_public = page.locator(f'select#{select_id}')

        options_public = [x.inner_text() for x in select_public.locator('option').all()]

        button_public = page.locator('a#ctl00_MainContent_ExportButton')

        data_public = []

        for label in options_public:

            select_public = page.locator(f'select#{select_id}')

            if label.lower() == 'all years':
                continue

            select_public.select_option(label=label)

            page.locator('a#ctl00_MainContent_SearchButton').click()

            table = page.locator('table#DataTables_Table_0')

            if 'no records found' in table.inner_text().lower():
                continue

            print(f'Grabbing public lobbyist data for {label}')

            results_select = page.locator('select[name="DataTables_Table_0_length"]')

            results_select.select_option(value="1000")

            table = page.locator('table#DataTables_Table_0')

            rows = table.locator('tbody > tr').all()

            for row in rows:
                year, name, agency = [x.inner_text() for x in row.locator('td').all()]

                last, rest = [x.strip() for x in name.split(',')]

                data = [year, last, rest, agency]

                data_public.append(
                    dict(zip(headers_public, data))
                )

            time.sleep(2)

        data_public.sort(
            key=lambda x: (
                x['year'],
                x['name_last'].lower(),
                x['name_first'].lower()
            )
        )

        with open(filepath_csv_public, 'w') as outfile:
            writer = csv.DictWriter(outfile, fieldnames=headers_public)
            writer.writeheader()
            writer.writerows(data_public)

        print(f'Wrote {str(filepath_csv_public)}')

        browser.close()

def assemble_private():

    data_processed = []
    str_current = ''

    # have to process the files one row at a time to
    # handle unescaped newlines
    for txt in dir_private.glob('*.txt'):
        with open(txt.resolve(), 'r', newline='') as infile:
            lines = infile.readlines()
            csv_headers = [x.strip() for x in lines[0].split('|')]

            for line in lines[1:]:
                if not str_current:
                    str_current = line

                ls = [' '.join(x.split()) for x in line.split('|')]
                firstval = ' '.join(ls[0].split())

                try:
                    # if it's a year, append str and start a new one
                    if int(firstval) and len(firstval) == 4:

                        # print(str_current)
                        row_data = [' '.join(x.split()) for x in str_current.split('|')]

                        if not any(row_data):
                            continue

                        data_processed.append(
                            dict(
                                zip(
                                    csv_headers,
                                    row_data
                                )
                            )
                        )
                        str_current = ' '.join(line.split())
                except ValueError:
                    # it's not a year, so it belongs with the previous line
                    str_current += ' ' + ' '.join(line.split()).strip()

    data_processed.sort(
        key=lambda x: (
            x['YEAR'],
            x['LOBBYIST_LAST_NAME'].lower(),
            x['LOBBYIST_FIRST_NAME'].lower()
        )
    )

    with open(filepath_csv_private, 'w') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=csv_headers)
        writer.writeheader()
        writer.writerows(data_processed)

    print(f'Wrote {filepath_csv_private}')


def build_readme():
    tmpl = '''# South Dakota lobbyist data

_Updated {updated_date}_

You can download data on South Dakota lobbyist registrations since {earliest_year} using [this state website](https://sosenterprise.sd.gov/BusinessServices/Lobbyist/LobbyistSearch.aspx), but a) you can't export all the data at once, b) the download for each year is a zipfile containing a single pipe-delimited text file, some with records that include unescaped newlines, and c) you can't export the public lobbyist information as data.

This project [uses `playwright`](download.py) to navigate the form and collect lobbyist registration data in two tidy files:
- [`south-dakota-lobbyists-private.csv`](south-dakota-lobbyists-private.csv) ({count_private:,} records)
- [`south-dakota-lobbyists-public.csv`](south-dakota-lobbyists-public.csv) ({count_public:,} records)
    '''

    with open(filepath_csv_private, 'r') as infile:
        data_private = list(csv.DictReader(infile))
        count_private = len(data_private)
        earliest_year_private = min([int(x['YEAR']) for x in data_private])

    with open(filepath_csv_public, 'r') as infile:
        data_public = list(csv.DictReader(infile))
        count_public = len(data_public)
        earliest_year_public = min([int(x['year']) for x in data_public])

    earliest_year = min([earliest_year_private, earliest_year_public])

    with open('README.md', 'w') as outfile:
        outfile.write(
            tmpl.format(
                updated_date=TODAY.strftime('%B %d, %Y'),
                earliest_year=earliest_year,
                count_public=count_public,
                count_private=count_private
            )
        )


if __name__ == '__main__':
    download()
    assemble_private()
    build_readme()
