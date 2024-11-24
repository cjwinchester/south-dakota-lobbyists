from pathlib import Path
from datetime import datetime
import time
import csv
import json
import itertools
import random
from urllib.parse import urljoin, urlparse, parse_qs
from email import utils

from playwright.sync_api import sync_playwright
import pdfplumber
import probablepeople as pp
import requests
from bs4 import BeautifulSoup
from scourgify import normalize_address_record
from scourgify.exceptions import (
    UnParseableAddressError,
    AddressNormalizationError
)


SEARCH_URL = 'https://sosenterprise.sd.gov/BusinessServices/Lobbyist/LobbyistSearch.aspx'

SELECTOR_BUTTON_SEARCH = '#ctl00_MainContent_SearchButton'
SELECTOR_BUTTON_PRINT = '#ctl00_MainContent_PrintButton'
SELECTOR_YEARS = '#ctl00_MainContent_slctYears'
SELECTOR_TABLE_ROWS = 'select[name="DataTables_Table_0_length"]'
SELECTOR_TABLE = 'table#DataTables_Table_0'
SELECTOR_LAST_NAME = '#ctl00_MainContent_txtLastName'

REQ_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0'
}

NOW = datetime.now()
THIS_YEAR = NOW.year

# registration records >= this year
# will be redownloaded to check for new info
FIRST_YEAR_DOWNLOAD = THIS_YEAR

config = {
    'private': {
        'pdf_vertical_lines': {
            'year': (0, 70),
            'expense_report_lobbyist': (70, 122),
            'expense_report_employer': (122, 180),
            'lobbyist_name': (180, 343),
            'employer': (343, 500),
            'status': (500,)
        }
    },
    'public': {
        'pdf_vertical_lines': {
            'year': (0, 75),
            'lobbyist_name': (75, 305),
            'agency': (305,)
        }
    }
}


for lobbyist_type in config.keys():
    folder = Path(lobbyist_type)
    filetype = 'csv' if lobbyist_type == 'public' else 'json'

    config[lobbyist_type]['selector_radio'] = f'#ctl00_MainContent_chkSearchBy{lobbyist_type.title()}'
    config[lobbyist_type]['dir'] = folder
    config[lobbyist_type]['filepath_pdf'] = folder / f'search-results-{lobbyist_type}.pdf'
    config[lobbyist_type]['filepath_data'] = folder / f'south-dakota-lobbyists-{lobbyist_type}.{filetype}'

    if lobbyist_type == 'private':
        config[lobbyist_type]['dir_pages'] = folder / 'detail-pages'
        config[lobbyist_type]['dir_last_names'] = folder / 'last-names'
        config[lobbyist_type]['dir_forms'] = folder / 'disclosure-forms'

FILEPATH_PARSED_NAMES = Path('private') / 'parsed-names.json'

with open(FILEPATH_PARSED_NAMES, 'r') as infile:
    parsed_names = json.load(infile)

# mapping probablepeople keys
name_key_map = {
    'FirstInitial': 'name_first',
    'GivenName': 'name_first',
    'MiddleInitial': 'name_middle',
    'MiddleName': 'name_middle',
    'Nickname': 'name_nickname',
    'PrefixOther': 'name_prefix',
    'SuffixGenerational': 'name_suffix',
    'SuffixOther': 'name_suffix',
    'Surname': 'name_last'
}

# read in some data error fixes
with open('fixes.json', 'r') as infile:
    fixes = json.load(infile)

# name fixes for probablepeople lookups
name_fixes = fixes.get('name_fixes')

# various registration date errors to fix
date_fixes = fixes.get('date_fixes')

# public lobbyists mistakenly included in private
# lobbyist data but missing from public data --
# we'll skip them when parsing the private data and
# add them to the public data
public_but_private = fixes.get('public_but_private')

FILEPATH_RSS = Path('south-dakota-lobbyists.xml')


class ResultsPDF:
    ''' A PDF exported from the S.D. Secretary
        of State's webite containing a table of data
        on state lobbyists
    '''
    def __init__(self, filepath):
        if not isinstance(filepath, Path):
            filepath = Path(filepath)

        self.filepath = filepath
        is_private = 'private' in str(filepath)

        self.report_type = 'private' if is_private else 'public'
        self.config = config[self.report_type]

        self.pdf = pdfplumber.open(self.filepath)

        self.gather_crops()

        self.data = []
        self.collect_data()

        self.pdf.close()

    def get_page_crops(self, page):
        ''' given a page, get cropped sections representing each record'''

        breaks = [
            # page top
            page.bbox[1],

            # page bottom
            page.bbox[3]
        ]

        # add the tops/bottoms of gray rectangles
        for rect in page.rects:
            breaks.append(rect['top'])
            breaks.append(rect['bottom'])

        # and sort
        breaks.sort()

        # get a list of overlapping pairs
        a, b = itertools.tee(breaks)
        next(b, None)

        # and build the coordinates
        coords = [(0, x[0], page.width, x[1]) for x in zip(a, b)]

        # list to gather crops
        crops = []

        for c in coords:
            crop = page.crop(c)
            if crop.extract_text():
                crops.append(crop)

        return {page.page_number: crops}

    def gather_crops(self):
        ''' process the PDF to gather a list of cropped sections, each representing a single record
         '''

        data_crops = {}

        for page in self.pdf.pages:

            if page.page_number == 1:

                # crop out top material on first page
                # by targeting the lines above the table
                bottom = page.lines[0].get('bottom') + 1

                page = page.crop(
                    (0, bottom, page.width, page.height)
                )

            crops = self.get_page_crops(page)
            data_crops = {**data_crops, **crops}

            page.close()

        self.data_crops = data_crops

        return self

    def parse_data_public(self):
        if self.report_type != 'public':
            return

        line_breaks = self.config['pdf_vertical_lines']

        for page_num in self.data_crops:
            for crop in self.data_crops[page_num]:

                d = {}

                for key in line_breaks:
                    breaks = line_breaks[key]

                    vertical_break_start = breaks[0]

                    if key == 'agency':
                        vertical_break_end = crop.width
                    else:
                        vertical_break_end = breaks[1]

                    section_crop = crop.crop(
                        (
                            vertical_break_start,
                            crop.bbox[1],
                            vertical_break_end,
                            crop.bbox[3]
                        )
                    )

                    section_text = section_crop.extract_text(layout=True).upper()

                    section_lines = [x.strip() for x in section_text.splitlines() if x.strip()]

                    section_text = ' '.join(
                        section_text.split()
                    )

                    if key == 'agency':

                        d['agency'] = ' '.join(section_lines[0].split())

                        agency_address = ' '.join(section_lines[1:])

                        agency_address = ' '.join(agency_address.split())

                        d['agency_address'] = agency_address
                        continue

                    d[key] = section_text

                self.data.append(d)

        # add public records mistakenly categorized as private
        self.data.extend(
            list(public_but_private.values())
        )

        self.data.sort(
            key=lambda x: (x['agency'], x['year'])
        )

        return self

    def parse_data_private(self):
        if self.report_type != 'private':
            return

        line_breaks = self.config['pdf_vertical_lines']
        data = []

        for page_num in self.data_crops:

            crops = self.data_crops[page_num]
            for crop in crops:

                d = {}

                for key in line_breaks:
                    breaks = line_breaks[key]

                    vertical_break_start = breaks[0]

                    if key == 'status':
                        vertical_break_end = crop.width
                    else:
                        vertical_break_end = breaks[1]

                    section_crop = crop.crop(
                        (
                            vertical_break_start,
                            crop.bbox[1],
                            vertical_break_end,
                            crop.bbox[3]
                        )
                    )

                    section_text = section_crop.extract_text(layout=True).upper()

                    section_lines = [x.strip() for x in section_text.splitlines() if x.strip()]

                    section_text = ' '.join(
                        section_text.split()
                    )

                    if key == 'lobbyist_name':
                        name = ' '.join(section_lines[0].split())

                        name = name_fixes.get(name, name)

                        address_lobbyist = ' '.join(
                            section_lines[1:]
                        )

                        d['address_lobbyist'] = ' '.join(
                            address_lobbyist.split()
                        )

                        parsed_name = parsed_names.get(name)

                        if parsed_name:
                            parsed_name['name_full'] = name
                            d['lobbyist_name'] =parsed_name
                            continue

                        if 'TEST ' in name:
                            d['skip'] = True
                            continue

                        try:
                            results = pp.tag(name)

                            if results[1] != 'Person':
                                raise Exception(f'Unparsed name: {name}')

                            data_out = {name_key_map.get(x): results[0].get(x) for x in results[0].keys()}

                            parsed_names[name] = data_out

                            data_out['name_full'] = name

                            d['lobbyist_name'] = data_out

                            continue

                        except pp.RepeatedLabelError:
                            raise Exception(f'Unparsed name: {name}')

                    d[key] = section_text

                if d.get('skip'):
                    continue

                self.data.append(d)

        self.data.sort(
            key=lambda x: (
                x.get('lobbyist_name').get('name_last'),
                x.get('lobbyist_name').get('name_first')
            )
        )

        with open(FILEPATH_PARSED_NAMES, 'w') as outfile:
            json.dump(
                parsed_names,
                outfile,
                indent=4
            )

        print(f'Wrote {str(FILEPATH_PARSED_NAMES.resolve())}')

        return self

    def collect_data(self):
        if not self.data_crops:
            self.gather_crops()

        if self.report_type == 'public':
            self.parse_data_public()

        if self.report_type == 'private':
            self.parse_data_private()

        return self

    def write_data(self):
        ''' only writing out public data at this stage '''
        if self.report_type != 'public':
            return

        if not self.data:
            self.collect_data()

        filepath_out = self.config['filepath_data'].resolve()

        with open(filepath_out, 'w', encoding='utf=8', newline='') as outfile:
            writer = csv.DictWriter(
                outfile,
                fieldnames=list(self.data[0].keys())
            )
            writer.writeheader()
            writer.writerows(self.data)

        print(f'- Wrote {len(self.data):,} records to {filepath_out}')

        return self

    def __str__(self):
        return self.filepath


def download_pdfs():
    ''' Downloads PDFs with lists of public and private lobbyists '''

    targets = [(config[x]['selector_radio'], config[x]['filepath_pdf']) for x in config]

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=False)
        page = browser.new_page()
        page.goto(SEARCH_URL, timeout=0)

        for pair in targets:
            page.locator(pair[0]).click()
            time.sleep(1)

            page.locator(SELECTOR_YEARS).select_option('0')

            page.locator(SELECTOR_BUTTON_SEARCH).click()
            time.sleep(1)

            with page.expect_download(timeout=0) as download_info:
                page.locator(SELECTOR_BUTTON_PRINT).click(timeout=0)

                download = download_info.value
                download.save_as(pair[1])

                print(f'Downloaded {pair[1]}')

        browser.close()

    return [x[1] for x in targets]


def get_detail_urls_private(last_names=[]):
    ''' loop over a list of last names to plug into
        the search page and scrape the data into an intermediate file in `last_names`
    '''

    dir_last_names = config['private']['dir_last_names']
    plural = 'name' if len(last_names) == 1 else 'names'

    print(f'Searching {len(last_names):,} {plural} ...')

    finished = {}

    try:
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=False)
            page = browser.new_page()
            page.goto(SEARCH_URL, timeout=0)

            for lname in last_names:
                print(f'Searching {lname} ...')

                page.locator(SELECTOR_LAST_NAME).fill(lname)
                page.locator(SELECTOR_YEARS).select_option('0')

                page.locator(SELECTOR_BUTTON_SEARCH).click()
                time.sleep(1)

                page.locator(SELECTOR_TABLE_ROWS).select_option('1000')

                table = page.locator(SELECTOR_TABLE)
                html = table.inner_html()

                soup = BeautifulSoup(html, 'html.parser')
                rows = soup.find_all('tr')[1:]

                if not rows:
                    raise Exception(f'No results for {lname}')

                plural = 'records' if len(rows) > 1 else 'record'

                print(f'- Found {len(rows):,} {plural}')

                registrations = []

                for row in rows:
                    (
                        year,
                        reg_no,
                        reg_status,
                        lobbyist_name,
                        lobbyist_city_state_zip,
                        lobbyist_phone_email,
                        employer,
                        employer_address,
                        employer_city_state_zip
                    ) = row.find_all('td')

                    link = reg_no.find('a').get('href')
                    url = urljoin(SEARCH_URL, link)

                    detail_page_deets = {
                        'year': int(year.text),
                        'registration_number': reg_no.text,
                        'url': url,
                        'registration_status': reg_status.text,
                        'lobbyist_name': lobbyist_name.text,
                        'lobbyist_city_state_zip': lobbyist_city_state_zip.text,
                        'lobbyist_phone_email': lobbyist_phone_email.text,
                        'employer': employer.text,
                        'employer_address': employer_address.text,
                        'employer_city_state_zip': employer_city_state_zip.text
                    }

                    registrations.append(detail_page_deets)

                urls_collected = {lname: registrations}

                finished = {
                    **finished,
                    **urls_collected
                }

                filepath_url_detail = dir_last_names / f'{lname}.json'

                with open(filepath_url_detail, 'w') as outfile:
                    json.dump(
                        urls_collected,
                        outfile,
                        indent=4
                    )

                print(f'- Wrote {filepath_url_detail}')
                print()

                time.sleep(random.uniform(1, 3))

            browser.close()
    except Exception as e:
        print(e)
        time.sleep(5)
        print('\n😅 Ope! Rebooting ...\n')

        unfinished = [x for x in last_names if x not in finished.keys()]

        random.shuffle(unfinished)
        get_detail_urls_private(last_names=unfinished)

    return finished


def scrape_registration_page(html_filepath):

    if not isinstance(html_filepath, Path):
        html_filepath = Path(html_filepath)

    with open(html_filepath, 'r') as infile:
        html = infile.read()

    soup = BeautifulSoup(html, 'html.parser')
    registration_guid = html_filepath.stem

    # skip if actually a public lobbyist
    if registration_guid in public_but_private.keys():
        return {}

    d = {
        'url': f'https://sosenterprise.sd.gov/BusinessServices/Lobbyist/LobbyistRegistrationDetail.aspx?CN={registration_guid}',
        'registration_guid': registration_guid
    }

    reg_id_block_text = soup.find(
        'span', 
        {'id': 'ctl00_MainContent_lblRegistrationNo'}
    ).text

    year, rest = reg_id_block_text.split('-')
    _, registration_num = rest.split(':')

    d['year'] = int(year)
    d['registration_number'] = ' '.join(registration_num.split())

    span_map = {
        'lobbyist_name': 'ctl00_MainContent_txtLobbyistName',
        'lobbyist_status': 'ctl00_MainContent_txtStatus',
        'lobbyist_employment_date': 'ctl00_MainContent_txtEmploymentDate',
        'lobbyist_phone': 'ctl00_MainContent_txtPhone',
        'lobbyist_email': 'ctl00_MainContent_txtEmail',
        'lobbyist_address': 'ctl00_MainContent_txtResidenceAddress',
        'lobbyist_occupation': 'ctl00_MainContent_txtOccupation',
        'lobbyist_type': 'ctl00_MainContent_txtType',
        'employer_name': 'ctl00_MainContent_txtEmployerName',
        'employer_agent_name': 'ctl00_MainContent_txtAgentName',
        'employer_registration_date': 'ctl00_MainContent_txtRegistrationDate',
        'employer_authorization_date': 'ctl00_MainContent_txtAuthorizationDate',
        'employer_lobbying_subjects': 'ctl00_MainContent_txtSubject',
        'employer_registration_status': 'ctl00_MainContent_txtRegistrationStatus',
        'employer_address': 'ctl00_MainContent_txtEmployerAddress'
    }

    for key in span_map:
        val = soup.find(
            'span',
            {'id': span_map[key]}
        )

        val_txt = ' '.join(
            val.text.split()
        ).upper()

        if val_txt and '_date' in key:
            val_txt = datetime.strptime(
                val_txt,
                '%m/%d/%Y'
            ).date().isoformat()

        if val and '_address' in key:
            val_txt = val.get_text(
                strip=True,
                separator='\n'
            ).splitlines()
            val_txt = ' '.join(val_txt).upper()

        d[key] = val_txt

    lobbyist_name = ' '.join(
        d['lobbyist_name'].split()
    ).upper()

    name_parsed = parsed_names.get(lobbyist_name)

    d['lobbyist_name'] = {
        'name_full': lobbyist_name,
        **name_parsed
    }

    try:
        parsed_address_lobbyist = normalize_address_record(
            d['lobbyist_address']
        )
        d['lobbyist_address'] = {
            'address_full': d['lobbyist_address'],
            **parsed_address_lobbyist
        }
    except (
        UnParseableAddressError, 
        AddressNormalizationError
    ):
        d['lobbyist_address'] = {
            'address_full': d['lobbyist_address']
        }

    try:
        parsed_address_employer = normalize_address_record(
            d['employer_address']
        )
        d['employer_address'] = {
            'address_full': d['employer_address'],
            **parsed_address_employer
        }
    except (
        UnParseableAddressError, 
        AddressNormalizationError
    ):
        d['employer_address'] = {
            'address_full': d['employer_address']
        }

    # apply date fixes, if any
    if date_fixes.get(d['registration_guid']):
        d = {
            **d,
            **date_fixes.get(d['registration_guid'])
        }

    table = soup.find('table')
    rows = table.find_all('tr')[1:]

    filings = []

    for row in rows:
        doc = {}

        # "filing detail", the last item in the row, is always blank
        filing_type, filing_date, document_number, _ = row.find_all('td')

        doc['filing_type'] = ' '.join(
            filing_type.text.split()
        )

        if filing_date:
            doc['filing_date'] = datetime.strptime(
                filing_date.text.strip(),
                '%m/%d/%Y'
            ).date().isoformat()

        doc['filing_number'] = ' '.join(
            document_number.text.split()
        )

        doc_link = document_number.find('a')

        if doc_link:
            document_url = urljoin(
                'https://sosenterprise.sd.gov/BusinessServices/Business/',
                Path(doc_link.get('href')).name
            )

            doc['filing_url'] = document_url

            document_url_parsed = urlparse(document_url)
            doc_id = parse_qs(document_url_parsed.query)['id'][0]

            doc['filing_guid'] = doc_id

            # download the filing if it doesn't
            # exist locally
            filepath_filing = config['private']['dir_forms'] / f'{doc_id}.pdf'

            if not filepath_filing.exists():
                with requests.get(
                    document_url,
                    headers=REQ_HEADERS,
                    stream=True
                ) as r, open(filepath_filing, 'wb') as fd:

                    r.raise_for_status()

                    for chunk in r.iter_content():
                        fd.write(chunk)

                time.sleep(random.uniform(1, 3))

                print(f'- Wrote {str(filepath_filing)}')

                doc['new'] = True

        filings.append(doc)

    d['filings'] = filings

    return d


def scrape_private_data():
    data_out = []
    new_filings = []

    for html_file in config['private']['dir_pages'].glob('*.html'):

        scraped_data = scrape_registration_page(html_file)

        # skip if this is actually a public lobbyist record
        if not scraped_data:
            continue

        lobbyist_name = scraped_data.get('lobbyist_name').get('name_full')
        employer_name = scraped_data.get('employer_name')

        for filing in scraped_data.get('filings'):
            if filing.get('new'):
                new_filings.append({
                    **filing,
                    **{
                        'lobbyist_name': lobbyist_name,
                        'employer_name': employer_name
                    }
                })
                del filing['new']

        data_out.append(scraped_data)

    # sort by `employer_registration_date`, the most consistent date for a registration record
    data_out.sort(
        key=lambda x: (
            x['year'],
            x['employer_registration_date']
        ),
        reverse=True
    )

    fpath = config['private']['filepath_data'].resolve()

    with open(fpath, 'w') as outfile:
        json.dump(
            data_out,
            outfile,
            indent=4
        )

    print(f'Wrote {str(fpath)}')

    return {
        'scraped_data': data_out,
        'new_filings': new_filings
    }


def build_readme():

    file_in, file_out = Path('readme.template'), Path('README.md')

    with open(file_in, 'r') as infile:
        tmpl = infile.read()

    with open(config['private']['filepath_data'], 'r') as infile:
        data_private = json.load(infile)

    zero_filings = []
    filings_count = 0
    employer_registration_dates = []

    for reg in data_private:

        empl_date = datetime.fromisoformat(
            reg['employer_registration_date']
        ).date()

        if empl_date.year > 2011 and empl_date.year <= THIS_YEAR:
            employer_registration_dates.append(empl_date)

        reg_no = reg.get('registration_number')
        filings = reg.get('filings')

        if not filings:
            zero_filings.append(reg)
            continue

        filings_count += len(reg.get('filings'))

    registrations_min_date = min(employer_registration_dates)
    registrations_max_date = max(employer_registration_dates)

    date_range_private = f'{registrations_min_date.isoformat()} to {registrations_max_date.isoformat()}'

    with open(config['public']['filepath_data'], 'r') as infile:
        data_public = list(csv.DictReader(infile))

    years = set([x.get('year') for x in data_public])
    date_range_public = f'{min(years)} to {max(years)}'

    to_replace = (
        ('{% UPDATED %}', NOW.strftime('%B %d, %Y')),
        ('{% COUNT_PRIVATE_REGISTRATIONS %}', f'{len(data_private):,}'),
        ('{% COUNT_PRIVATE_REGISTRATION_NO_FILINGS %}', f'{len(zero_filings):,}'),
        ('{% COUNT_PRIVATE_FILINGS %}', f'{filings_count:,}'),
        ('{% DATE_RANGE_PRIVATE %}', date_range_private),
        ('{% COUNT_PUBLIC_REGISTRATIONS %}', f'{len(data_public):,}'),
        ('{% DATE_RANGE_PUBLIC %}', date_range_public),
    )

    for pair in to_replace:
        tmpl = tmpl.replace(*pair)

    with open(file_out, 'w') as outfile:
        outfile.write(tmpl)

    print(f'- Wrote {str(file_out)}')

    return file_out


def download_detail_pages(urls=[]):
    ''' given a list of URLs for registration
        detail pages, download each page that
        hasn't already been downloaded

        return a list of newly downloaded registrations
    '''

    '''
    # to read from local files instead ...
    urls = []

    for d in config['private']['dir_last_names'].glob('*.json'):
        with open(d, 'r') as infile:
            registration_data = json.load(infile)
        for x in registration_data:
            urls.extend(
                [x.get('url') for x in registration_data[x]]
            )
    '''

    new_downloads = []

    for url in set(urls):
        parsed_url = urlparse(url)
        registration_id = parse_qs(parsed_url.query)['CN'][0]

        detail_page_filepath = (config['private']['dir_pages'] / f'{registration_id}.html').resolve()

        if detail_page_filepath.exists():
            continue

        req = requests.get(
            url,
            headers=REQ_HEADERS
        )

        req.raise_for_status()

        time.sleep(random.uniform(1, 3))

        with open(detail_page_filepath, 'w') as outfile:
            outfile.write(req.text)

        print(f'- Wrote {detail_page_filepath}')

        new_downloads.append(registration_id)

    return new_downloads


def vet_results_private(scraped_data=[], pdf_data=[]):
    ''' compare `pdf_data`, which is canon, with `scraped_data`

    The list of lobbyist names, and the years they've lobbied, should match

    returns True if it doesn't throw
    '''

    lookup_scraped = {}
    for record in scraped_data:
        name_full = record.get('lobbyist_name').get('name_full')
        if not lookup_scraped.get(name_full):
            lookup_scraped[name_full] = []

        lookup_scraped[name_full].append(record.get('year'))

    lookup_pdf = {}
    for record in pdf_data:
        name_full = record.get('lobbyist_name').get('name_full')
        if not lookup_pdf.get(name_full):
            lookup_pdf[name_full] = []

        lookup_pdf[name_full].append(record.get('year'))

    # make sure all the registration records are present
    for name in lookup_pdf:
        skip_names = [
            # variously listed as public lobbyists
            'ANN BOLMAN',
            'TIFFANY SANDERSON',

            # error introduced in 11/24 -- his name is attached to other lobbyists' records
            'MARK SNEDEKER'
        ]

        if name in skip_names:
            continue

        pdf_years = ', '.join(
            sorted(
                [str(x) for x in lookup_pdf[name]]
            )
        )
        scraped_years = ', '.join(
            sorted(
                [str(x) for x in lookup_scraped[name]]
            )
        )

        if pdf_years != scraped_years:
            msg = f'Missing registration data for {name}\nScraped from PDF: {pdf_years}\nScraped from website: {scraped_years}'

            raise Exception(msg)

    return True


def build_rss(items=[]):
    if not items:
        return

    with open('rss.template', 'r') as infile:
        tmpl = infile.read()

    build_date = utils.format_datetime(NOW)
    item_str = ''

    for item in items:
        item_str += f'''
    <item>
      <title>{item.get('title')}</title>
      <link>{item.get('link')}</link>
      <description>{item.get('description')}</description>
      <pubDate>{item.get('pub_date')}</pubDate>
      <guid isPermaLink="false">{item.get('guid')}</guid>
    </item>
        '''

    rpl = (
        ('{% BUILD_DATE %}', build_date),
        ('{% ITEMS %}', item_str)
    )

    for pair in rpl:
        tmpl = tmpl.replace(*pair)

    with open(FILEPATH_RSS, 'w') as outfile:
        outfile.write(tmpl)

    print(f'- Wrote {FILEPATH_RSS}')


if __name__ == '__main__':

    download_pdfs()

    print('\nProcessing public lobbyist file ...')
    public_lobbyists = ResultsPDF(
        config['public']['filepath_pdf']
    )
    public_lobbyists.write_data()

    print('\nProcessing private lobbyist file ...')
    private_lobbyists = ResultsPDF(
        config['private']['filepath_pdf']
    )

    print(f'- Parsed {len(private_lobbyists.data):,} records\n')

    # only re-download last name search results from `FIRST_YEAR_DOWNLOAD` onward
    lnames_to_search = set([x.get('lobbyist_name')['name_last'] for x in private_lobbyists.data if int(x['year']) >= FIRST_YEAR_DOWNLOAD])

    finished = get_detail_urls_private(
        last_names=sorted(lnames_to_search)
    )

    # collect the URLs of registration detail pages
    # for `FIRST_YEAR_DOWNLOAD` onward
    urls = []
    
    for name in finished:
        urls.extend(
            [x.get('url') for x in finished[name] if x.get('year') >= FIRST_YEAR_DOWNLOAD]
        )

    # this function returns a list of guids for registration pages downloaded this time around
    new_registration_pages = download_detail_pages(urls=urls)

    # scrape the private lobbyist data
    scraped = scrape_private_data()

    # verify that every record in the PDF is present in
    # the scraped data
    vet_results_private(
        pdf_data=private_lobbyists.data,
        scraped_data=scraped.get('scraped_data')
    )

    # rebuild RSS feed if there's anything new
    rss_items = []

    new_registrations = [x for x in scraped.get('scraped_data') if x.get('registration_guid') in new_registration_pages]

    for rec in new_registrations:
        rss_items.append({
            'title': f'Lobbyist registration: {rec.get("lobbyist_name").get("name_full").replace('&', '&#x26;')} for {rec.get("employer_name").replace('&', '&#x26;')}',
            'link': rec.get('url'),
            'description': rec.get('employer_lobbying_subjects').replace('&', '&#x26;'),
            'pub_date': utils.format_datetime(
                datetime.fromisoformat(
                    rec.get('employer_registration_date')
                )
            ),
            'guid': rec.get('registration_guid')
        })

    for filing in scraped.get('new_filings'):
        rss_items.append({
            'title': f'Lobbyist filing {filing.get("filing_number")}: {filing.get("filing_type")} filed by {filing.get("lobbyist_name").replace('&', '&#x26;')} for {filing.get("employer_name").replace('&', '&#x26;')}',
            'link': filing.get('filing_url'),
            'pub_date': utils.format_datetime(
                datetime.fromisoformat(
                    filing.get('filing_date')
                )
            ),
            'guid': filing.get('filing_guid')
        })

    build_rss(items=rss_items)
    build_readme()
