
import urllib2
import bs4
import codecs
import os
import re
import xlsxwriter
import contextlib
import subprocess
from collections import defaultdict, namedtuple

BASE_URL = 'https://www.gov.uk/employment-tribunal-decisions?tribunal_decision_categories%%5B%%5D=age-discrimination&page=%d'

type_re = re.compile(r'.*?-\s*([^-]*)$')
case_number_swap_space_re = re.compile(r'\b([A-Z0-9]{6,}?)[ /]{0,2}((?:20)?[0-9][0-9])\b')
case_number_re = re.compile(r'\b((?:S/)?[A-Z0-9]{4,}/(?:20)?[0-9]{2})\b')

ignored_names = frozenset([
])

workbook = xlsxwriter.Workbook('EmploymentTribunalDecisions.xlsx')
worksheet_final = workbook.add_worksheet('UKET Final')
worksheet_prelim = workbook.add_worksheet('UKET Prelim')

CODES = (
        u'Age Discrimination',
        u'Agency Workers',
        u'Breach of Contract',
        u'Contract of Employment',
        u'Disability Discrimination',
        u'Equal Pay Act',
        u'Fixed Term Regulations',
        u'Flexible Working',
        u'Health & Safety',
        u'Interim Relief',
        u'Jurisdictional Points',
        u'Maternity and Pregnancy Rights',
        u'National Minimum Wage',
        u'Parental and Maternity Leave',
        u'Part Time Workers',
        u'Protective Award',
        u'Public Interest Disclosure',
        u'Race Discrimination',
        u'Redundancy',
        u'Religion or Belief Discrimination',
        u'Renumeration',
        u'Reorganisation',
        u'Right To Be Accompanied',
        u'Sex Discrimination',
        u'Sexual Orientation Discrimination/Transexualism',
        u'Statutory Discipline and Grievance Procedures',
        u'Time Off',
        u'Trade Union Membership',
        u'Trade Union Rights',
        u'Transfer of Undertakings',
        u'Unfair Dismissal',
        u'Unlawful Deduction from Wages',
        u'Victimisation Discrimination',
        u'Working Time Regulations',
        u'Written Pay Statement',
        u'Written Statements',
)
def code_to_id(code):
    assert code in CODES
    return 'code_%d' % CODES.index(code)
def id_to_code(id):
    assert id.startswith('code_')
    return CODES[int(id[5:])]

Entry = namedtuple('Entry', ['case_number', 'decision_date', 'country', 'citation', 'name', 'male', 'female', 'type', 'withdrawn', 'judge', 'claimant_counsel', 'respondent_counsel', 'num_decisions', 'final_on_page', 'text'] + [code_to_id(code) for code in CODES])

all_entries = []

def list_rindex(li, x):
    for i in reversed(range(len(li))):
        if li[i] == x:
            return i
    return -1

def list_index(li, arg, start=None):
    if start is None or start < 0:
        start = 0
    if isinstance(arg, str):
        pred = lambda x: x == arg
    else:
        pred = arg
    for i in range(len(li)):
        if i < start:
            continue
        if pred(li[i]):
            return i
    return -1

def get_name(s):
    s = s.strip()
    if len(s) == 0:
        return False
    upper_bits = []
    for bit in s.split():
        if bit[0].isupper() or bit.startswith("d'"):
            upper_bits.append(bit)
        else:
            break
    if len(upper_bits) > 0:
        return ' '.join(upper_bits)
    else:
        return None

def asciify(s):
    s = s.strip('.')
    return s.decode('ascii','ignore').encode('ascii').strip()

judge_re1 = re.compile(r"before: (?:employment )?(?:judge )?([A-Z.a-z' -]+)", re.I)
judge_re2 = re.compile(r"^([A-Z][A-Z.a-z' -]* )[eE]mployment [jJ]udge$", re.M | re.I)
judge_re3 = re.compile(r"^[eE]mployment [jJ]udge:?((?:[ \t]+[A-Z][A-Z.a-z'-]*)+)$", re.M | re.I)
judge_re4 = re.compile(r"employment judge((?:[ \t]+[A-Z][A-Z.a-z' -]*)+)", re.M | re.I)
def find_judge(data):
    # Find a line such as 'BEFORE: Employment Judge X Y Wibblester (sitting alone)'
    g = judge_re1.search(data)
    if g:
        name = g.group(1).strip()
        name = get_name(name)
        if name:
            return name

    # Find a line such as 'A M Foo Employment Judge'
    g = judge_re2.search(data)
    if g:
        name = g.group(1).strip()
        if name.endswith('made by'):
            name = ''
        name = get_name(name)
        if name:
            return name

    # Find a line such as 'Employment Judge Foo'
    g = judge_re3.search(data)
    if g:
        name = g.group(1).strip()
        name = get_name(name)
        if name:
            return name

    # Employment Judge:
    # Date of Judgment:
    # Entered in register:
    # and copied to parties
    # 25
    #
    # 30
    #
    # 35
    #
    # A Kwong
    # 5 March 2015
    # 11 March 2015
    lines = [s.strip() for s in data.splitlines()]
    ej_index = list_rindex(lines, 'Employment Judge:')
    if ej_index >= 0:
        tail_lines = lines[ej_index:]
        if tail_lines[1].lower() == 'date of judgment:' and \
                tail_lines[2].lower().startswith('entered in') and \
                tail_lines[3].lower() == 'and copied to parties':
            for line in tail_lines[4:]:
                if line == '':
                    continue
                if len(line) <= 3:
                    continue
                return line

    g = [get_name(s) for s in judge_re4.findall(data) if get_name(s) is not None]
    if not g:
        return ''
    s = reduce(lambda x, y: x if len(x) < len(y) else y, g)
    return s

def find_withdrawn(data):
    rule52_re = re.compile('rule\s*52', re.I)
    withdrawn_re = re.compile('withdraw', re.I)
    if rule52_re.search(data) and withdrawn_re.search(data):
        return 1
    return 0

def has_representation(lines):
    rep_re = re.compile('^(?:appearances|represented\s+by|representation|representatives)\s*:?$', re.I)
    for line in lines:
        if rep_re.match(line):
            return True
    return False


def in_sequence(*args):
    if len(args) == 0:
        return True
    for i in range(len(args)-1):
        if args[i] is None:
            return False
        if args[i] + 1 != args[i + 1]:
            return False
    return True

def find_counsel(data, judge):
    lines = [s.strip() for s in data.splitlines() if s.strip()]

    # Remove any lines that look like a date or a judge.
    date_re = re.compile(r'.*\b(?:january|february|march|april|may|june|july|august|september|october|november|december)+\s+(?:20)?[0-9]{2}', re.I)
    on_re = re.compile(r'^on\s*:', re.I)
    judge_re = re.compile(r'(?:employment\s+)?judge\s*:?', re.I)
    lines = [x for x in lines if not date_re.match(x) and not judge_re.match(x) and not on_re.match(x)]
    if judge:
        the_judge_re = re.compile(re.escape(judge), re.I)
        lines = [x for x in lines if not the_judge_re.search(x)]

    # Terminate after a line containing "JUDGEMENT"
    judgement_line = list_index(lines, lambda s: re.match("JUDGE?MENT", s))
    if judgement_line >= 0:
        lines = lines[:judgement_line]

    # First find layout like this:
    # Appearances:
    # For the Claimant:
    # Mr M Smith, legal executive
    # For the Respondent:
    # Mr A Jones, counsel
    a = list_index(lines, lambda s: s.lower().startswith('appearances'))
    ftc = list_index(lines, lambda s: re.match(r'for (?:the )?(?:first )?claimants?:?\s*$', s, re.I))
    ftr = list_index(lines, lambda s: re.match(r'for (?:the )?(?:first )?respondents?:?\s*$', s, re.I))
    if in_sequence(ftc, ftr - 1) and len(lines) > ftr+1:
        return lines[ftc+1], lines[ftr+1]

    # Or this:
    # Appearances:
    # For the Claimant: no appearance
    # For the Respondent: Miss R Rabbit, counsel
    rep_re = re.compile('^(?:appearances|representation|representatives)\s*:?$', re.I)
    rep = list_index(lines, lambda s: rep_re.match(s))
    ftcs_re = '(?:for )(?:the )?(?:first )?claimants?:?\s+([^\s].*)'
    ftrs_re = '(?:for )(?:the )?(?:first )?respondents?:?\s+([^\s].*)'
    ftcs = list_index(lines, lambda s: re.match(ftcs_re, s, re.I))
    ftrs = list_index(lines, lambda s: re.match(ftrs_re, s, re.I))
    c_re = r'(?:for (?:the )?)?(?:first )?claimants?:?\s*(.*)'
    r_re = r'(?:for (?:the )?)?(?:first )?respondents?:?\s*(.*)'
    c = list_index(lines, lambda s: re.match(c_re, s, re.I), rep)
    r = list_index(lines, lambda s: re.match(r_re, s, re.I), rep)
    on = list_index(lines, lambda s: s.lower() == 'on:', rep)
    # Appearances :
    # Claimant In person
    # For the Respondent Ms S Fish Solicitor
    if in_sequence(rep, c, r):
        the_claimant = re.match(c_re, lines[c], re.I).group(1).strip()
        the_respondent = re.match(r_re, lines[r], re.I).group(1).strip()
        if the_claimant and the_respondent and \
                the_claimant != lines[c] and the_respondent != lines[r]:
            return the_claimant, the_respondent

    # Or this:
    # Representation
    # Claimant:
    # Respondent:
    # On:
    # 5th September 2018
    # Mr B Cabbage (Solicitor)
    # Mr P Sausage (Counsel)
    if in_sequence(rep, c, r, on) and len(lines) > on+3:
        return lines[on+2], lines[on+3]

    # Or this - same as above, without on:
    # Representation
    # Claimant:
    # Respondent:
    # Did not attend
    # Mr D Silverbeet, Solicitor
    if in_sequence(rep, c, r) and len(lines) > r+2:
        return lines[r+1], lines[r+2]

    # Or this
    # Representation
    # Claimant:
    # Did not attend
    # Respondent: Mr P Soup, solicitor
    if in_sequence(rep, c, ftrs-1) and len(lines) > c+1:
        the_respondent = re.match(ftrs_re, lines[ftrs], re.I).group(1).strip()
        return lines[c+1], the_respondent

    # Or this
    # Representation
    # Claimant:
    # Did not attend
    # Respondent:
    # Mr P Soup, solicitor
    if in_sequence(rep, c, r-1) and len(lines) > r+1:
        return lines[c+1], lines[r+1]

    # Or find this:
    # APPEARANCES:
    # For the Claimant:
    # For the Respondent:
    # Self-represented
    # Mr D Flatted (Counsel)
    if in_sequence(a, ftc, ftr) and len(lines) > ftr+2:
        return lines[ftr+1], lines[ftr+2]

    # Or find this:
    # Claimant
    # Represented by:
    # Mr M Munch Counsel
    # Some Seafood Limited
    # Respondents
    # Represented by:
    # Mrs Y M Seatbelt
    # Law at Home
    # Inc Empire
    sanitize = lambda s: s.lower().rstrip('.: \t')
    one_liner_reps = ('in person', 'no attendance', 'no appearance', 'did not attend', 'not represented')
    c_nocolon = list_index(lines, lambda s: sanitize(s) in ('claimant', 'claimants'))
    c_inperson = list_index(lines, lambda s: sanitize(s) in one_liner_reps, c_nocolon)
    c_repby = list_index(lines, lambda s: sanitize(s) == 'represented by', c_nocolon)
    c_repby_more = list_index(lines, lambda s: sanitize(s).startswith('represented by'), c_nocolon)
    r_nocolon = list_index(lines, lambda s: sanitize(s) in ('respondents', 'respondent', '1st respondent', 'first respondent'), c_nocolon)
    r_inperson = list_index(lines, lambda s: sanitize(s) in one_liner_reps, r_nocolon)
    r_repby = list_index(lines, lambda s: sanitize(s) == 'represented by', r_nocolon)
    r_repby_more = list_index(lines, lambda s: sanitize(s).startswith('represented by'), r_nocolon)
    def decode(nocolon, inperson, repby, repby_more):
        if in_sequence(nocolon, inperson):
            return lines[inperson].strip(':')
        elif 0 <= nocolon < repby and repby - nocolon < 4:
            return lines[repby + 1]
        elif nocolon >= 0 and repby_more >= 0 and repby_more - nocolon < 3:
            rep = lines[repby_more][len("represented by"):].strip('.: \t')
            if rep == '-': rep = 'in person'
            return rep
        else:
            return ''
    c_rep = decode(c_nocolon, c_inperson, c_repby, c_repby_more)
    r_rep = decode(r_nocolon, r_inperson, r_repby, r_repby_more)
    if c_rep or r_rep:
        return c_rep, r_rep

    if not has_representation(lines):
        return '-','-'

    return '',''

total_pages = 0
for i in range(1, 25):
    #print "%d... " % i,
    # Fetch top-level pages.
    if not os.path.exists('data/%d.html' % i):
        print 'Fetching page %d' % i
        io = urllib2.urlopen(BASE_URL % i)
        f = open('data/%d.html' % i, 'w')
        f.write(io.read())
        io.close()
        f.close()

    f = open('data/%d.html' % i, 'r')
    outersoup = bs4.BeautifulSoup(f.read(), 'html.parser')
    f.close()
    for entry in outersoup.select('li a'):
        url = entry.attrs.get('href')
        g = re.compile(r'^/employment-tribunal-decisions/(.*)$').match(url)
        if g is None:
            continue
        name = g.group(1)
        output_dir = os.path.join('data', name)
        output_html = os.path.join(output_dir, 'index.html')
        decision_url = 'https://www.gov.uk/' + url
        if not os.path.exists(output_html):
            try:
                os.mkdir(output_dir)
            except OSError:
                pass
            if 'karina-bappa' in name:
                # Skipped, missing?
                continue
            print 'Fetching:', name
            with contextlib.closing(urllib2.urlopen(decision_url)) as io:
                with open(output_html, 'w') as f:
                    f.write(io.read())

        if name in ignored_names:
            continue

        f = codecs.open(output_html, 'r', 'utf-8')
        soup = bs4.BeautifulSoup(f.read(), 'html.parser')
        f.close()

        decision_date = ''
        code_ids = set()
        for date_tag in soup.select('dt.app-c-important-metadata__term'):
            if 'Decision date' in date_tag.get_text():
                for sibling in date_tag.next_siblings:
                    if sibling.name == 'dd':
                        decision_date = sibling.string.strip()
                        break
            if 'Country' in date_tag.get_text():
                for sibling in date_tag.next_siblings:
                    if sibling.name == 'dd':
                        country = sibling.string.strip()
                        break
            if 'Jurisdiction code' in date_tag.get_text():
                for sibling in date_tag.next_siblings:
                    if sibling.name == 'dd':
                        for code_tag in sibling.select('a'):
                            code = code_tag.get_text()
                            code_ids.add(code)
                        break

        title = soup.select('title')[0].string.strip()
        print repr(title)
        case_numbers = []
        extracted_case_numbers = case_number_re.findall(title)
        if not extracted_case_numbers:
            extracted_case_numbers = case_number_re.findall(
                    case_number_swap_space_re.sub(r'\1/\2', title)
            )
        for m in extracted_case_numbers:
            case_numbers.append(m)
        case_number = ' '.join(case_numbers)
        print '\t',repr(case_numbers)

        header_text = soup.select('h1.gem-c-title__text')[0].get_text().strip()

        header = '=HYPERLINK("%s", "%s")' % (decision_url, header_text)
        # X v Y [mmmm] UKET nnnn/mmmm
        def build_citation(header, case_numbers):
            year = decision_date.split(' ')[-1]
            whos = header_text.split(':', 1)[0]
            who_bits = whos.split(' v ', 1)
            who_re = re.compile(r'.*?\b([A-Z][a-zA-Z]*)$')
            def fix_who(who):
                who = who.strip()
                m = who_re.match(who)
                if m:
                    return m.group(1)
                else:
                    return who
            who_bits = [fix_who(who_bits[0]), who_bits[1]]
            bits = [
                who_bits[0],
                'v',
                who_bits[1],
            ]
            bits.append('[{}]'.format(year))
            bits.append('UKET')
            bits.append(case_numbers[0])
            return ' '.join(bits)
        citation = build_citation(header_text, case_numbers)
        #print repr(citation)


        male_re = re.compile(r'\b(?:Mr)\b', re.I)
        female_re = re.compile(r'\b(?:Ms|Miss|Mrs)\b', re.I)
        unknown_re = re.compile(r'\bDr\b', re.I)
        male = 1 if male_re.match(header_text) else 0
        female = 1 if female_re.match(header_text) else 0
        unknown = 1 if unknown_re.match(header_text) else 0

        attachments = []
        entries = []
        for attachment in soup.select('span.attachment-inline a'):
            attachment_url = attachment.attrs.get("href")
            g = type_re.search(attachment.string)
            claim_type = g.group(1).strip().strip('.') if g else ''
            fn = os.path.basename(attachment_url)
            attachment_file = os.path.join(output_dir, fn)
            if attachment_file.endswith('.pdf'):
                if not os.path.exists(attachment_file):
                    print 'Fetching attachment:', attachment_url
                    with contextlib.closing(urllib2.urlopen(attachment_url)) as io:
                        with open(attachment_file, 'w') as f:
                            f.write(io.read())
            else:
                if not os.path.exists(attachment_file):
                    attachment_file = attachment_file.rsplit('.', 1)[0] + '.pdf'
                else:
                    raise Exception('Unhandled attachment: ' + attachment_file)

            output_textfile = attachment_file[:-4] + '.txt'
            if not os.path.exists(output_textfile):
                subprocess.check_call(["pdftotext", attachment_file])

            attachment_data = open(output_textfile, 'r').read()
            # Fix quotes
            attachment_data = attachment_data.replace('\u8217',"'").replace('\xe2\x80\x99',"'")

            attachments.append(output_textfile)
            #url = '=HYPERLINK("%s", "[pdf]")' % (attachment_url)

            pdfinfo = subprocess.check_output(["pdfinfo", attachment_file])
            pages = int(re.search('Pages:\s*([0-9]+)', pdfinfo).group(1).strip())
            total_pages += pages

            entry_dict = {code_to_id(c):(1 if c in code_ids else '') for c in CODES}
            entry_dict['case_number'] = case_number
            entry_dict['decision_date'] = decision_date
            entry_dict['citation'] = citation
            entry_dict['country'] = country
            entry_dict['name'] = header
            entry_dict['type'] = claim_type
            entry_dict['male'] = male
            entry_dict['female'] = female
            entry_dict['judge'] = asciify(find_judge(attachment_data))
            entry_dict['withdrawn'] = find_withdrawn(attachment_data)
            entry_dict['claimant_counsel'], entry_dict['respondent_counsel'] = map(asciify, find_counsel(attachment_data, entry_dict['judge']))
            #print attachment_file + ':' + entry_dict['claimant_counsel'] + ':' + entry_dict['respondent_counsel']
            entry_dict['num_decisions'] = 0 # Updated below
            entry_dict['final_on_page'] = 0 # Updated below
            entry_dict['text'] = asciify(attachment_data)
            entries.append(entry_dict)
        for e in entries:
            e['num_decisions'] = len(entries)
        if len(entries) > 0:
            entries[-1]['final_on_page'] = 1
        all_entries.extend([Entry(**d) for d in entries])

print total_pages

row = 0
for col, col_name in enumerate(Entry._fields):
    if col_name.startswith('code_'):
        col_name = id_to_code(col_name)
    worksheet_final.write(row, col, col_name)
    worksheet_prelim.write(row, col, col_name)
row_final = row + 1
row_prelim = row + 1

for e in sorted(all_entries, key=lambda x: x.case_number, reverse=True):
    for col, col_name in enumerate(Entry._fields):
        if e.final_on_page:
            worksheet_final.write(row_final, col, getattr(e, col_name))
        else:
            worksheet_prelim.write(row_prelim, col, getattr(e, col_name))
    if e.final_on_page:
        row_final += 1
    else:
        row_prelim += 1

workbook.close()
