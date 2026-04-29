#!/usr/bin/env python3
"""
ICEGATE OOC PDF Parser
Extracts structured data from ICEGATE-generated Out of Charge (OOC) documents.
Usage: python3 parse_ooc.py <path_to_ooc.pdf> [output_dir]
"""

import re, sys, json, sqlite3, os, subprocess
from pathlib import Path


# ──────────────────────────────────────────────
# TEXT EXTRACTION
# ──────────────────────────────────────────────

import pdfplumber

def extract_text(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
    return text


# ──────────────────────────────────────────────
# PART I PARSERS
# ──────────────────────────────────────────────

def parse_header(page1: str) -> dict:
    d = {}
    m = re.search(r'Port Code\s+BE No\s+BE Date\s+BE Type', page1)
    if m:
        rest = page1[m.end():m.end()+300]
        m2 = re.search(r'(\w+)\s+(\d{5,})\s+(\d{2}/\d{2}/\d{4})\s+(\w)', rest)
        if m2:
            d['port_code'] = m2.group(1)
            d['be_no']     = m2.group(2)
            d['be_date']   = m2.group(3)
            d['be_type']   = m2.group(4)

    for pat, key in [
        (r'IEC/Br\s+([\d/]+)',     'iec'),
        (r'GSTIN/TYPE\s+([\w/]+)', 'gstin'),
        (r'CB CODE\s+(\w+)',       'cb_code'),
        (r'OOC NO\.\s+(\d+)',      'ooc_no'),
        (r'OOC DATE\s+([\d\-]+)',  'ooc_date'),
    ]:
        m = re.search(pat, page1)
        if m: d[key] = m.group(1)

    m = re.search(r'TYPE\s+INV\s+ITEM\s+CONT\s*\nPORT.*?Nos\s+(\d+)\s+(\d+)\s+(\d+)', page1)
    if m:
        d['inv_count']  = int(m.group(1))
        d['item_count'] = int(m.group(2))
        d['cont_count'] = int(m.group(3))

    m = re.search(r'PKG\s+([\d.]+)\s+G\.WT \(KGS\)\s+([\d.]+)', page1)
    if m:
        d['pkg']     = m.group(1)
        d['gwt_kgs'] = m.group(2)

    return d


def parse_status(page1: str) -> dict:
    d = {}
    m = re.search(r'13\.COUNTRY OF ORIGIN\s+(\S.*?)\s{3,}14\.COUNTRY OF CONSIGNMENT\s+(\S.*)', page1)
    if m:
        d['country_of_origin']      = m.group(1).strip()
        d['country_of_consignment'] = m.group(2).strip()
    m = re.search(r'15\.PORT OF LOADING\s+(\S.*?)\s{3,}16\.PORT OF SHIPMENT\s+(\S.*)', page1)
    if m:
        d['port_of_loading']  = m.group(1).strip()
        d['port_of_shipment'] = m.group(2).strip()
    return d


def parse_duty_summary(page1: str) -> dict:
    d = {'fine': '0'}
    # BCD  0  SWS  NCCD  ADD  CVD  IGST  G.CESS  TOT_ASS_VAL
    m = re.search(
        r'(\d[\d.]+)\s+0\s+([\d.]+)\s+\S*\s*0\s+0\s+([\d.]+)\s+0\s+([\d.]+)',
        page1
    )
    if m:
        d['bcd'] = m.group(1); d['sws'] = m.group(2)
        d['igst'] = m.group(3); d['tot_ass_val'] = m.group(4)

    td_idx = page1.find('14.TOTAL DUTY')
    if td_idx > 0:
        after = page1[td_idx:td_idx+300]
        nums = re.findall(r'\d{6,}', after)
        if nums:           d['total_duty'] = nums[0]
        if len(nums) >= 2: d['tot_amount'] = nums[1]

    d.setdefault('tot_amount', d.get('total_duty', ''))
    return d


def parse_manifest(page1: str) -> dict:
    d = {}
    idx = page1.find('D.MANIFEST')
    if idx < 0: idx = page1.find('1.IGM NO')
    if idx < 0: return d
    section = page1[max(0, idx-50):idx+400]

    m = re.search(r'(\d{5,})\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})', section)
    if m:
        d['igm_no'] = m.group(1); d['igm_date'] = m.group(2); d['inw_date'] = m.group(3)

    m = re.search(r'([A-Z]{3,}\w*\d{4,})\s+(\d{2}/\d{2}/\d{4})', section)
    if m:
        d['mawb_no'] = m.group(1); d['mawb_date'] = m.group(2)

    m = re.search(r'(\d{3,})\s+([\d.]+)\s*(?:\n|$)', section)
    if m:
        d['pkg'] = m.group(1); d['gw'] = m.group(2)
    return d


def parse_invoices(page1: str) -> list:
    invoices = []
    idx = page1.find('INVOICE DETAILS - SUMMARY')
    if idx < 0: return invoices
    section = page1[idx:idx+1500]
    for m in re.finditer(
        r'(\d{1,2})\s+([\w\-\/]+)\s+([\d.]+)\s+(USD|GBP|EUR|INR|JPY|AUD|CAD)',
        section
    ):
        invoices.append({'sno': int(m.group(1)), 'invoice_no': m.group(2),
                         'inv_amt': m.group(3), 'currency': m.group(4)})
    return invoices


def parse_containers(page1: str) -> list:
    containers = []
    idx = page1.find('CONTAINER DETAILS')
    if idx < 0: return containers
    section = page1[idx:idx+600]
    for m in re.finditer(r'(\d+)\s+(F|L)\s+(\S*)\s+(\d{6,})\s+([A-Z]{4}\d{7})', section):
        containers.append({
            'sno': m.group(1),
            'type': 'FCL' if m.group(2) == 'F' else 'LCL',
            'seal': m.group(4),
            'container_no': m.group(5)
        })
    return containers


# ──────────────────────────────────────────────
# PART III ITEMS PARSER  (global position method)
# ──────────────────────────────────────────────

def parse_items(full_text: str) -> list:
    """
    Use global positions of all '1.INVSNO 2.ITEMSN ...' headers across the full text.
    This avoids duplicate counting from overlapping Part III section windows.
    Each occurrence maps to exactly one item.
    """
    items = []
    item_header_re = re.compile(
        r'1\.INVSNO\s+2\.ITEMSN\s+3\.CTH\s+4\.CETH\s+5\.ITEM DESCRIPTION'
    )
    positions = [m.start() for m in item_header_re.finditer(full_text)]

    for i, pos in enumerate(positions):
        # Block: from this header to next header (capped at 2500 chars)
        next_pos = positions[i+1] if i+1 < len(positions) else pos + 2500
        block = full_text[pos: min(next_pos, pos + 2500)]
        item = _parse_item_block(block)
        if item:
            items.append(item)

    return items


def _parse_item_block(block: str) -> dict:
    item = {}

    # Match data row: INVSNO  ITEMSNO  CTH(8 digits)  CETH  DESCRIPTION
    m = re.search(
        r'^\s{5,20}(\d{1,3})\s+(\d{1,3})\s+(\d{8})\s+(\S+)\s+(.+?)(?:\s{4,}|$)',
        block, re.MULTILINE
    )
    if not m:
        return {}

    item['inv_sno']  = int(m.group(1))
    item['item_sno'] = int(m.group(2))
    item['cth']      = m.group(3)
    item['ceth']     = m.group(4)
    desc = m.group(5).strip()
    # Catch split description continuation
    rest = block[m.end():]
    cont = re.match(r'\s{20,}([A-Z0-9 ()\-/\.,]+)', rest)
    if cont and len(cont.group(1).strip()) > 3:
        desc += ' ' + cont.group(1).strip()
    item['item_desc'] = desc

    # ── Parse 29.ASSESS VALUE and 30. TOTAL DUTY ──
    # Format: header line then (possibly several blank lines due to page breaks) then data line
    # e.g.:   29.ASSESS VALUE   30. TOTAL DUTY\n  [junk/blank]\n  ...  4291.67  1886.6
    av_idx = block.find('29.ASSESS VALUE')
    if av_idx >= 0:
        av_section = block[av_idx:av_idx+500]
        lines = av_section.split('\n')
        found = False
        for j, line in enumerate(lines):
            if '29.ASSESS VALUE' in line:
                # Search up to 10 subsequent lines for a line with decimal numbers > 10
                for k in range(j + 1, min(j + 12, len(lines))):
                    nums = re.findall(r'\d+\.\d+', lines[k])
                    # Filter: must be float, and first value must be > 10 (real monetary value)
                    valid = [float(n) for n in nums if float(n) > 10]
                    if valid:
                        item['assess_value'] = valid[0]
                        item['total_duty']   = valid[1] if len(valid) > 1 else 0.0
                        found = True
                        break
                break

    return item


# ──────────────────────────────────────────────
# VERIFICATION
# ──────────────────────────────────────────────

def verify_totals(data: dict) -> dict:
    items = data['items']
    sum_assess = round(sum(i.get('assess_value', 0) for i in items), 2)
    sum_duty   = round(sum(i.get('total_duty',   0) for i in items), 2)
    try:    rep_assess = float(data['duty_summary'].get('tot_ass_val') or 0)
    except: rep_assess = 0
    try:    rep_duty   = float(data['duty_summary'].get('total_duty') or 0)
    except: rep_duty   = 0
    return {
        'items_parsed':         len(items),
        'declared_item_count':  data['header'].get('item_count', 0),
        'sum_assess_value':     sum_assess,
        'sum_total_duty':       sum_duty,
        'reported_tot_ass_val': rep_assess,
        'reported_total_duty':  rep_duty,
        'assess_match':         abs(sum_assess - rep_assess) < 50.0,
        'duty_match':           abs(sum_duty   - rep_duty)   < 50.0,
    }


# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS be_header (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    port_code TEXT, be_no TEXT, be_date TEXT, be_type TEXT,
    iec TEXT, gstin TEXT, cb_code TEXT,
    inv_count INTEGER, item_count INTEGER, cont_count INTEGER,
    pkg TEXT, gwt_kgs TEXT, ooc_no TEXT, ooc_date TEXT
);
CREATE TABLE IF NOT EXISTS be_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    be_no TEXT, country_of_origin TEXT, country_of_consignment TEXT,
    port_of_loading TEXT, port_of_shipment TEXT
);
CREATE TABLE IF NOT EXISTS be_duty_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    be_no TEXT, bcd TEXT, sws TEXT, igst TEXT,
    tot_ass_val TEXT, total_duty TEXT, fine TEXT, tot_amount TEXT
);
CREATE TABLE IF NOT EXISTS be_manifest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    be_no TEXT, igm_no TEXT, igm_date TEXT, inw_date TEXT,
    mawb_no TEXT, mawb_date TEXT, pkg TEXT, gw TEXT
);
CREATE TABLE IF NOT EXISTS be_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    be_no TEXT, sno INTEGER, invoice_no TEXT, inv_amt TEXT, currency TEXT
);
CREATE TABLE IF NOT EXISTS be_containers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    be_no TEXT, sno TEXT, type TEXT, seal TEXT, container_no TEXT
);
CREATE TABLE IF NOT EXISTS be_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    be_no TEXT, inv_sno INTEGER, item_sno INTEGER,
    cth TEXT, ceth TEXT, item_desc TEXT,
    assess_value REAL, total_duty REAL
);
"""


def save_to_db(data: dict, db_path: str):
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()
    cur.executescript(SCHEMA)
    be_no = data['header'].get('be_no', 'UNKNOWN')
    h, s, d, m = data['header'], data['status'], data['duty_summary'], data['manifest']

    cur.execute("INSERT INTO be_header VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h.get('port_code'), be_no, h.get('be_date'), h.get('be_type'),
         h.get('iec'), h.get('gstin'), h.get('cb_code'),
         h.get('inv_count'), h.get('item_count'), h.get('cont_count'),
         h.get('pkg'), h.get('gwt_kgs'), h.get('ooc_no'), h.get('ooc_date')))

    cur.execute("INSERT INTO be_status VALUES (NULL,?,?,?,?,?)",
        (be_no, s.get('country_of_origin'), s.get('country_of_consignment'),
         s.get('port_of_loading'), s.get('port_of_shipment')))

    cur.execute("INSERT INTO be_duty_summary VALUES (NULL,?,?,?,?,?,?,?,?)",
        (be_no, d.get('bcd'), d.get('sws'), d.get('igst'),
         d.get('tot_ass_val'), d.get('total_duty'), d.get('fine'), d.get('tot_amount')))

    cur.execute("INSERT INTO be_manifest VALUES (NULL,?,?,?,?,?,?,?,?)",
        (be_no, m.get('igm_no'), m.get('igm_date'), m.get('inw_date'),
         m.get('mawb_no'), m.get('mawb_date'), m.get('pkg'), m.get('gw')))

    for inv in data['invoices']:
        cur.execute("INSERT INTO be_invoices VALUES (NULL,?,?,?,?,?)",
            (be_no, inv['sno'], inv['invoice_no'], inv['inv_amt'], inv['currency']))

    for c in data['containers']:
        cur.execute("INSERT INTO be_containers VALUES (NULL,?,?,?,?,?)",
            (be_no, c['sno'], c['type'], c['seal'], c['container_no']))

    for item in data['items']:
        cur.execute("INSERT INTO be_items VALUES (NULL,?,?,?,?,?,?,?,?)",
            (be_no, item.get('inv_sno'), item.get('item_sno'),
             item.get('cth'), item.get('ceth'), item.get('item_desc'),
             item.get('assess_value', 0), item.get('total_duty', 0)))

    conn.commit(); conn.close()


# ──────────────────────────────────────────────
# EXCEL EXPORT
# ──────────────────────────────────────────────

def export_excel(data: dict, xlsx_path: str):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        os.system("pip install openpyxl --break-system-packages -q")
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb    = openpyxl.Workbook()
    be_no = data['header'].get('be_no', 'OOC')

    HDR_FILL = PatternFill("solid", fgColor="1F3864")
    HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
    SUB_FILL = PatternFill("solid", fgColor="2E75B6")
    SUB_FONT = Font(color="FFFFFF", bold=True, size=9)
    ALT_FILL = PatternFill("solid", fgColor="D6E4F0")
    BDR      = Border(left=Side(style='thin'), right=Side(style='thin'),
                      top=Side(style='thin'),  bottom=Side(style='thin'))

    def hdr_cell(ws, r, c, val):
        cell = ws.cell(r, c, val)
        cell.fill = HDR_FILL; cell.font = HDR_FONT; cell.border = BDR
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    def kv_block(ws, pairs, start_r):
        for i, (k, v) in enumerate(pairs):
            r = start_r + i
            a = ws.cell(r, 1, k); a.font = Font(bold=True); a.border = BDR
            b = ws.cell(r, 2, v if v is not None else ''); b.border = BDR
        return start_r + len(pairs)

    def section_title(ws, r, title):
        ws.merge_cells(f'A{r}:B{r}')
        ws[f'A{r}'] = title
        ws[f'A{r}'].fill = SUB_FILL; ws[f'A{r}'].font = SUB_FONT
        ws[f'A{r}'].alignment = Alignment(horizontal='center')
        return r + 1

    # ── Sheet 1: Summary ──────────────────────
    ws = wb.active; ws.title = "Summary"
    ws.column_dimensions['A'].width = 30
    ws.column_dimensions['B'].width = 32
    ws.merge_cells('A1:B1')
    ws['A1'] = f"ICEGATE OOC DOCUMENT  ·  BE No: {be_no}"
    ws['A1'].font = Font(bold=True, size=13, color="1F3864")
    ws['A1'].alignment = Alignment(horizontal='center')

    h, s, d, m = data['header'], data['status'], data['duty_summary'], data['manifest']

    r = 3
    r = section_title(ws, r, "PART I — BILL OF ENTRY HEADER")
    r = kv_block(ws, [
        ("Port Code",       h.get('port_code')),   ("BE No",         h.get('be_no')),
        ("BE Date",         h.get('be_date')),      ("BE Type",       h.get('be_type')),
        ("IEC",             h.get('iec')),          ("GSTIN/TYPE",    h.get('gstin')),
        ("CB Code",         h.get('cb_code')),
        ("INV Count (TYPE)", h.get('inv_count')),   ("ITEM Count",    h.get('item_count')),
        ("PKG",             h.get('pkg')),          ("G.WT (KGS)",    h.get('gwt_kgs')),
        ("OOC No",          h.get('ooc_no')),       ("OOC Date",      h.get('ooc_date')),
    ], r) + 1

    r = section_title(ws, r, "A. STATUS")
    r = kv_block(ws, [
        ("13. Country of Origin",      s.get('country_of_origin')),
        ("14. Country of Consignment", s.get('country_of_consignment')),
        ("15. Port of Loading",        s.get('port_of_loading')),
        ("16. Port of Shipment",       s.get('port_of_shipment')),
    ], r) + 1

    r = section_title(ws, r, "C. DUTY SUMMARY")
    r = kv_block(ws, [
        ("BCD",             d.get('bcd')),
        ("SWS",             d.get('sws')),
        ("IGST",            d.get('igst')),
        ("18. TOT.ASS VAL", d.get('tot_ass_val')),
        ("14. TOTAL DUTY",  d.get('total_duty')),
        ("17. FINE",        d.get('fine')),
        ("19. TOT. AMOUNT", d.get('tot_amount')),
    ], r) + 1

    r = section_title(ws, r, "D. MANIFEST DETAILS")
    kv_block(ws, [
        ("1. IGM NO",   m.get('igm_no')),    ("2. IGM DATE", m.get('igm_date')),
        ("3. INW DATE", m.get('inw_date')),  ("6. MAWB NO",  m.get('mawb_no')),
        ("7. DATE",     m.get('mawb_date')), ("10. PKG",     m.get('pkg')),
        ("11. GW",      m.get('gw')),
    ], r)

    # ── Sheet 2: Invoice Summary ──────────────
    ws2 = wb.create_sheet("I. Invoice Summary")
    for col_l, w in [('A',6),('B',28),('C',15),('D',8)]:
        ws2.column_dimensions[col_l].width = w
    for c, txt in enumerate(['S.NO','INVOICE NO','INV. AMT','CUR'], 1):
        hdr_cell(ws2, 1, c, txt)
    for ri, inv in enumerate(data['invoices'], 2):
        fill = ALT_FILL if ri % 2 == 0 else None
        for ci, val in enumerate([inv.get('sno'),inv.get('invoice_no'),
                                   inv.get('inv_amt'),inv.get('currency')], 1):
            c = ws2.cell(ri, ci, val); c.border = BDR
            if fill: c.fill = fill

    # ── Sheet 3: Container Details ────────────
    ws3 = wb.create_sheet("J. Container Details")
    for col_l, w in [('A',6),('B',8),('C',15),('D',18)]:
        ws3.column_dimensions[col_l].width = w
    for c, txt in enumerate(['SNO','TYPE','4. SEAL','5. CONTAINER NO'], 1):
        hdr_cell(ws3, 1, c, txt)
    for ri, cont in enumerate(data['containers'], 2):
        for ci, val in enumerate([cont.get('sno'),cont.get('type'),
                                   cont.get('seal'),cont.get('container_no')], 1):
            ws3.cell(ri, ci, val).border = BDR

    # ── Sheet 4: All Items (Part III) ─────────
    ws4 = wb.create_sheet("Part III — All Items")
    col_widths = {'A':8,'B':8,'C':12,'D':14,'E':62,'F':16,'G':14,'H':12}
    for col_l, w in col_widths.items():
        ws4.column_dimensions[col_l].width = w
    ws4.row_dimensions[1].height = 32
    item_hdrs = ['1. INV SNO','2. ITEM SN','CTH','CETH',
                 '5. ITEM DESCRIPTION','29. ASSESS VALUE','30. TOTAL DUTY','BE NO']
    for c, txt in enumerate(item_hdrs, 1):
        hdr_cell(ws4, 1, c, txt)

    for ri, item in enumerate(data['items'], 2):
        fill = ALT_FILL if ri % 2 == 0 else None
        vals = [item.get('inv_sno'), item.get('item_sno'),
                item.get('cth'), item.get('ceth'), item.get('item_desc'),
                item.get('assess_value',''), item.get('total_duty',''), be_no]
        for ci, val in enumerate(vals, 1):
            cell = ws4.cell(ri, ci, val); cell.border = BDR
            if fill: cell.fill = fill
            if ci in (6, 7) and isinstance(val, (int, float)):
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal='right')

    # ── Sheet 5: Verification ─────────────────
    ws5 = wb.create_sheet("Verification")
    ws5.column_dimensions['A'].width = 35
    ws5.column_dimensions['B'].width = 20
    ws5.column_dimensions['C'].width = 15
    ws5.merge_cells('A1:C1')
    ws5['A1'] = "TOTALS VERIFICATION REPORT"
    ws5['A1'].fill = HDR_FILL; ws5['A1'].font = HDR_FONT
    ws5['A1'].alignment = Alignment(horizontal='center')

    v = data.get('verification', {})
    rows = [
        ("Items Parsed",              v.get('items_parsed', len(data['items'])), ""),
        ("Declared Item Count",       v.get('declared_item_count',''),           ""),
        ("Invoices Parsed",           len(data['invoices']),                     ""),
        ("",                          "",                                         ""),
        ("29. Sum of Assessable Values (all items)",
            v.get('sum_assess_value',''),
            "✅ MATCH" if v.get('assess_match') else "⚠️ MISMATCH"),
        ("18. Reported TOT.ASS VAL",  v.get('reported_tot_ass_val',''),          ""),
        ("",                          "",                                         ""),
        ("30. Sum of Total Duties (all items)",
            v.get('sum_total_duty',''),
            "✅ MATCH" if v.get('duty_match') else "⚠️ MISMATCH"),
        ("14. Reported TOTAL DUTY",   v.get('reported_total_duty',''),           ""),
    ]
    for i, (label, val, note) in enumerate(rows, 2):
        ws5.cell(i, 1, label).font = Font(bold=True)
        ws5.cell(i, 2, val)
        nc = ws5.cell(i, 3, note)
        if "MATCH" in str(note):      nc.font = Font(color="00B050", bold=True)
        elif "MISMATCH" in str(note): nc.font = Font(color="FF0000", bold=True)
        for col in [1, 2, 3]:
            ws5.cell(i, col).border = BDR

    wb.save(xlsx_path)
    print(f"[+] Excel saved → {xlsx_path}")


# ──────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ──────────────────────────────────────────────

def parse_ooc_pdf(pdf_path: str) -> dict:
    print(f"\n{'='*62}")
    print(f"  ICEGATE OOC Parser  |  {Path(pdf_path).name}")
    print(f"{'='*62}")

    full_text = extract_text(pdf_path)
    print(f"[+] Extracted {len(full_text):,} chars | {len(full_text.splitlines()):,} lines")

    ff    = full_text.find('\x0c')
    page1 = full_text[:ff] if ff > 0 else full_text[:5000]

    print("[+] Parsing Part I header, status, duty, manifest, invoices, containers...")
    header       = parse_header(page1)
    status       = parse_status(page1)
    duty_summary = parse_duty_summary(page1)
    manifest     = parse_manifest(page1)
    invoices     = parse_invoices(page1)
    containers   = parse_containers(page1)
    print(f"    BE={header.get('be_no')}  INV={header.get('inv_count')}  ITEMS={header.get('item_count')}")

    print("[+] Parsing Part III - all items (entire document)...")
    items = parse_items(full_text)
    print(f"    Parsed {len(items)} items")

    data = dict(header=header, status=status, duty_summary=duty_summary,
                manifest=manifest, invoices=invoices, containers=containers, items=items)
    data['verification'] = verify_totals(data)
    return data


if __name__ == '__main__':
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else '/mnt/user-data/uploads/3.pdf'
    out_dir  = sys.argv[2] if len(sys.argv) > 2 else '/tmp/output'
    os.makedirs(out_dir, exist_ok=True)

    data  = parse_ooc_pdf(pdf_path)
    be_no = data['header'].get('be_no', 'OOC')

    json_path = f"{out_dir}/ooc_{be_no}.json"
    with open(json_path, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"[+] JSON  → {json_path}")

    db_path = f"{out_dir}/ooc_data.db"
    save_to_db(data, db_path)
    print(f"[+] DB    → {db_path}")

    xlsx_path = f"{out_dir}/ooc_{be_no}_report.xlsx"
    export_excel(data, xlsx_path)

    v = data['verification']
    print(f"\n{'='*62}")
    print("  VERIFICATION SUMMARY")
    print(f"{'='*62}")
    print(f"  Declared Items    : {v['declared_item_count']}")
    print(f"  Items Parsed      : {v['items_parsed']}")
    print(f"  Invoices Parsed   : {len(data['invoices'])}")
    print(f"  Containers Parsed : {len(data['containers'])}")
    print(f"  {'─'*48}")
    print(f"  Sum Assess Val    : {v['sum_assess_value']:>16,.2f}")
    print(f"  Reported Ass Val  : {v['reported_tot_ass_val']:>16,.2f}  {'✅' if v['assess_match'] else '⚠️  MISMATCH'}")
    print(f"  Sum Total Duty    : {v['sum_total_duty']:>16,.2f}")
    print(f"  Reported Duty     : {v['reported_total_duty']:>16,.2f}  {'✅' if v['duty_match'] else '⚠️  MISMATCH'}")
    print(f"{'='*62}")
    print(f"\n  Output files in: {out_dir}/")
    print(f"  ├── ooc_{be_no}.json")
    print(f"  ├── ooc_data.db")
    print(f"  └── ooc_{be_no}_report.xlsx")
