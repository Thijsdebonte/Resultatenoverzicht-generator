from flask import Flask, render_template, request, jsonify, send_file
import csv, io, os, base64
from datetime import datetime, date

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'faam-dev-secret-2024')

# ── Dutch month abbreviations ────────────────────────────────
DUTCH_MONTHS = ['jan', 'feb', 'mrt', 'apr', 'mei', 'jun',
                'jul', 'aug', 'sep', 'okt', 'nov', 'dec']

# ── Faam colour palette ──────────────────────────────────────
GREEN  = (39, 174, 96)
DARK   = (26, 26, 26)
WHITE  = (255, 255, 255)
LGRAY  = (242, 242, 242)
BORDER = (224, 224, 224)
MUTED  = (110, 110, 110)

# ── Page dimensions (16:9 landscape) ────────────────────────
PAGE_W = 297   # mm
PAGE_H = 167   # mm


# ════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════

def fmt_date(date_str):
    """'2026-02-12'  →  '12 feb 2026'"""
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d')
        return f"{d.day} {DUTCH_MONTHS[d.month - 1]} {d.year}"
    except Exception:
        return date_str


def fmt_number(n):
    """90601  →  '90.601'  (Dutch thousands separator)"""
    try:
        return f"{int(n):,}".replace(',', '.')
    except Exception:
        return str(n)


def fmt_decimal(n, decimals=2):
    """32.08  →  '32,08'  (Dutch decimal separator)"""
    try:
        return f"{float(n):.{decimals}f}".replace('.', ',')
    except Exception:
        return str(n)


def timeline_data(start_str, end_str):
    """Returns positions (0-100) on a year timeline for start/end dates."""
    try:
        start = datetime.strptime(start_str, '%Y-%m-%d').date()
        end   = datetime.strptime(end_str,   '%Y-%m-%d').date()
        year  = start.year
        y0    = date(year, 1, 1)
        y1    = date(year + 1, 1, 1)
        total = (y1 - y0).days

        def pos(d):
            return round((d - y0).days / total * 100, 2)

        months = [{'pos': pos(date(year, m, 1)), 'name': DUTCH_MONTHS[m - 1]}
                  for m in range(1, 13)]

        return {
            'year':      year,
            'next_year': year + 1,
            'start_pos': pos(start),
            'end_pos':   pos(end),
            'months':    months,
        }
    except Exception:
        return None


def _decode_img(data_url):
    """Decode a base64 data-URL to a BytesIO object."""
    if not data_url or ',' not in data_url:
        return None
    try:
        _, data = data_url.split(',', 1)
        return io.BytesIO(base64.b64decode(data))
    except Exception:
        return None


# ════════════════════════════════════════════════════════════
#  CSV PARSERS
# ════════════════════════════════════════════════════════════

def parse_clarity_csv(content):
    if content.startswith('﻿'):
        content = content[1:]

    rows = list(csv.reader(content.splitlines()))
    data = {
        'date_range': '', 'campaign': '',
        'total_sessions': 0, 'scroll_depth': 0.0,
        'avg_time': 0, 'pages': [],
    }

    i = 0
    while i < len(rows):
        row = [c.strip().strip('"') for c in rows[i]]
        if not any(row):
            i += 1
            continue

        if row[0] == 'Datumbereik' and len(row) > 1:
            data['date_range'] = row[1]
        if row[0] == 'Campagne' and len(row) > 1:
            data['campaign'] = row[1]

        if row[0] == 'Metrisch' and len(row) > 1:
            section = row[1]
            j = i + 1
            sec_rows = []
            while j < len(rows):
                r = [c.strip().strip('"') for c in rows[j]]
                if not any(r) or r[0] == 'Metrisch':
                    break
                sec_rows.append(r)
                j += 1

            if section == 'Sessies':
                for r in sec_rows:
                    if len(r) >= 3 and 'Totaal aantal sessies' in r[1]:
                        try:
                            data['total_sessions'] = int(r[2].replace('.', '').replace(',', ''))
                        except Exception:
                            pass

            elif section == 'Schuifdiepte':
                for r in sec_rows:
                    if len(r) >= 3 and r[1] == 'Gemiddeld':
                        try:
                            data['scroll_depth'] = float(r[2].replace(',', '.'))
                        except Exception:
                            pass

            elif section == 'Actieve tijd besteed':
                for r in sec_rows:
                    if len(r) >= 3 and r[1] == 'Actieve tijd':
                        try:
                            data['avg_time'] = int(r[2])
                        except Exception:
                            pass

            elif 'Toppagina' in section:
                for r in sec_rows:
                    if len(r) >= 3 and r[1].startswith('http'):
                        try:
                            data['pages'].append({
                                'url': r[1],
                                'sessions': int(r[2].replace('.', '').replace(',', '')),
                            })
                        except Exception:
                            pass

        i += 1

    return data


def parse_meta_csv(content):
    if content.startswith('﻿'):
        content = content[1:]

    ad_sets = []
    for row in csv.DictReader(io.StringIO(content)):
        name = row.get('Naam advertentieset', '').strip()
        if not name:
            continue
        try:
            impressions = int(float(row.get('Weergaven', 0) or 0))
        except Exception:
            impressions = 0
        try:
            reach = int(float(row.get('Bereik', 0) or 0))
        except Exception:
            reach = 0
        ad_sets.append({
            'name':       name,
            'start_date': row.get('Start rapportage', ''),
            'end_date':   row.get('Einde rapportage', ''),
            'impressions': impressions,
            'reach':       reach,
        })

    return ad_sets


# ════════════════════════════════════════════════════════════
#  PDF GENERATION  (fpdf2 — pure Python, works on Vercel)
# ════════════════════════════════════════════════════════════

def _rounded_rect(pdf, x, y, w, h, r, corners='1234', style='F'):
    """Draw a rounded rect; falls back to plain rect if fpdf2 version is too old."""
    try:
        pdf.rounded_rect(x, y, w, h, r, corners, style=style)
    except AttributeError:
        pdf.rect(x, y, w, h, style=style)


def _make_pdf(data):
    from fpdf import FPDF

    pdf = FPDF(unit='mm', format=(PAGE_W, PAGE_H))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)

    _cover_page(pdf, data)

    logo_bytes = _decode_img(data.get('logo'))
    for v in data.get('vacatures', []):
        _results_page(pdf, v, logo_bytes)

    return bytes(pdf.output())


def _cover_page(pdf, data):
    pdf.add_page()

    photo_w = round(PAGE_W * 0.55, 1)   # ~163 mm
    right_x = photo_w
    right_w = PAGE_W - photo_w           # ~134 mm

    # Dark right panel
    pdf.set_fill_color(*DARK)
    pdf.rect(right_x, 0, right_w, PAGE_H, style='F')

    # Cover photo (drawn after the dark panel so it overlaps only the left side)
    cover = _decode_img(data.get('cover_photo'))
    if cover:
        pdf.image(cover, x=0, y=0, w=photo_w, h=PAGE_H)

    # "Faam." wordmark on the photo
    pdf.set_font('Helvetica', 'B', 52)
    pdf.set_text_color(*WHITE)
    pdf.set_xy(8, PAGE_H - 30)
    pdf.cell(photo_w - 30, 22, 'Faam.', ln=0)

    # Green decorative circle (to the right of the period)
    dot = 14
    pdf.set_fill_color(*GREEN)
    pdf.ellipse(photo_w - 22, PAGE_H - 24, dot, dot, style='F')

    # Title on right side
    pdf.set_text_color(*WHITE)
    pdf.set_font('Helvetica', 'B', 21)
    pdf.set_xy(right_x + 10, 18)
    pdf.multi_cell(right_w - 14, 11, 'Wervingsrapport\nvoor', align='L')

    # Client name in green
    pdf.set_text_color(*GREEN)
    pdf.set_font('Helvetica', 'B', 24)
    pdf.set_xy(right_x + 10, pdf.get_y() + 2)
    pdf.multi_cell(right_w - 14, 12, data.get('klant_naam', ''), align='L')

    # Contact info (bottom-right)
    pdf.set_font('Helvetica', '', 8)
    contact_y = PAGE_H - 28
    for label, value in [
        ('Telefoon', '0342-420741'),
        ('E-mail',   'marketing@faam.nl'),
        ('Website',  'www.faam.nl'),
    ]:
        pdf.set_xy(right_x + 10, contact_y)
        pdf.set_text_color(*GREEN)
        pdf.cell(22, 5.5, label, ln=0)
        pdf.set_text_color(*WHITE)
        pdf.cell(right_w - 36, 5.5, value, ln=1)
        contact_y += 6


def _results_page(pdf, v, logo_bytes=None):
    pdf.add_page()
    BX = 8   # body left/right margin (mm)
    HDR_H = 12

    # ── Header ──────────────────────────────────────────────
    if logo_bytes:
        logo_bytes.seek(0)
        pdf.image(logo_bytes, x=BX, y=2, h=8, w=0)
    else:
        pdf.set_font('Helvetica', 'B', 13)
        pdf.set_text_color(*DARK)
        pdf.set_xy(BX, (HDR_H - 5) / 2)
        pdf.cell(18, 5, 'Faam', ln=0)
        pdf.set_text_color(*GREEN)
        pdf.cell(5, 5, '.', ln=0)

    # Function title (centre, green)
    title_x = BX + 28
    title_w = PAGE_W - title_x - 72
    pdf.set_font('Helvetica', 'B', 11)
    pdf.set_text_color(*GREEN)
    pdf.set_xy(title_x, (HDR_H - 5) / 2)
    pdf.cell(title_w, 5, v.get('titel', ''), align='C', ln=0)

    # Date-range pill (right)
    date_str = f"{v.get('fmt_start', '')} - {v.get('fmt_end', '')}"
    pill_w = 66
    pill_h = 6
    pill_x = PAGE_W - pill_w - 6
    pill_y = (HDR_H - pill_h) / 2
    pdf.set_fill_color(240, 240, 240)
    pdf.set_draw_color(*BORDER)
    pdf.set_line_width(0.2)
    _rounded_rect(pdf,pill_x, pill_y, pill_w, pill_h, 1.5, '1234', style='FD')
    pdf.set_font('Helvetica', '', 7)
    pdf.set_text_color(*MUTED)
    pdf.set_xy(pill_x, pill_y)
    pdf.cell(pill_w, pill_h, date_str, align='C', ln=0)

    # Header divider
    pdf.set_draw_color(*BORDER)
    pdf.set_line_width(0.3)
    pdf.line(0, HDR_H, PAGE_W, HDR_H)

    # ── Body ────────────────────────────────────────────────
    y = HDR_H + 3

    # Heading
    pdf.set_font('Helvetica', 'B', 17)
    pdf.set_text_color(*DARK)
    pdf.set_xy(BX, y)
    pdf.cell(PAGE_W - BX * 2, 9, 'Een overzicht van de', ln=0)
    y += 9
    pdf.set_xy(BX, y)
    pdf.cell(PAGE_W - BX * 2, 9, 'belangrijkste resultaten.', ln=0)
    y += 11

    # Timeline
    tl = v.get('timeline')
    if tl:
        y = _draw_timeline(pdf, tl, y, BX)
    y += 2

    # Section 1 — Clarity / Faam.nl
    y = _draw_section(pdf, 'Algemene informatie Faam.nl', [
        ('Hoeveel mensen op de vacaturepagina zijn gekomen',
         v.get('fmt_sessions', '0')),
        ('Hoelang mensen gemiddeld op de pagina hebben gezeten',
         v.get('fmt_time', '0 sec')),
        ('Hoe ver mensen gemiddeld op de pagina scrollen',
         v.get('fmt_scroll', '0%')),
    ], y, BX)

    y += 2

    # Section 2 — Meta ads
    y = _draw_section(pdf, 'Algemene informatie vacatureadvertenties', [
        ('Hoe vaak vacatureadvertenties zijn weergegeven',
         v.get('fmt_impressions', '0')),
        ('Hoeveel unieke mensen de advertentie hebben gezien',
         v.get('fmt_reach', '0')),
    ], y, BX)

    y += 2

    # Section 3 — Sollicitaties
    _draw_section(pdf, 'Sollicitaties', [
        ('Aantal sollicitaties', str(v.get('sollicitaties', 0))),
    ], y, BX, max_card_w=55)


def _draw_timeline(pdf, tl, y, bx):
    lx  = bx + 16
    lw  = PAGE_W - bx - 16 - bx
    ly  = y + 5

    # Year labels
    pdf.set_font('Helvetica', '', 7)
    pdf.set_text_color(*MUTED)
    pdf.set_xy(bx, y + 2)
    pdf.cell(16, 4, str(tl['year']), ln=0)
    pdf.set_xy(PAGE_W - bx - 14, y + 2)
    pdf.cell(14, 4, str(tl['next_year']), align='R', ln=0)

    # Base line
    pdf.set_draw_color(180, 180, 180)
    pdf.set_line_width(0.4)
    pdf.line(lx, ly, lx + lw, ly)

    # Month ticks + labels
    for m in tl['months']:
        mx = lx + (m['pos'] / 100) * lw
        pdf.set_draw_color(175, 175, 175)
        pdf.set_line_width(0.15)
        pdf.line(mx, ly - 1.2, mx, ly + 1.2)
        pdf.set_font('Helvetica', '', 5.5)
        pdf.set_text_color(*MUTED)
        pdf.set_xy(mx - 3.5, ly + 1.8)
        pdf.cell(7, 3, m['name'], align='C', ln=0)

    # Start / end dots
    pdf.set_fill_color(*GREEN)
    pdf.set_draw_color(*WHITE)
    pdf.set_line_width(0.4)
    for pos_pct in (tl['start_pos'], tl['end_pos']):
        mx = lx + (pos_pct / 100) * lw
        r = 2
        pdf.ellipse(mx - r, ly - r, r * 2, r * 2, style='FD')

    return ly + 6   # new y position


def _draw_section(pdf, title, metrics, y, bx, max_card_w=None):
    cw = PAGE_W - bx * 2   # content width

    # Divider line + title
    pdf.set_draw_color(*BORDER)
    pdf.set_line_width(0.25)
    pdf.line(bx, y, PAGE_W - bx, y)
    y += 1.5

    pdf.set_font('Helvetica', 'B', 7.5)
    pdf.set_text_color(*DARK)
    pdf.set_xy(bx, y)
    pdf.cell(cw, 5, title, ln=0)
    y += 7

    # Cards
    n      = len(metrics)
    gap    = 3
    card_w = (cw - gap * (n - 1)) / n
    if max_card_w:
        card_w = min(float(max_card_w), card_w)
    card_h = 24

    for i, (label, value) in enumerate(metrics):
        cx = bx + i * (card_w + gap)
        cy = y

        # Card background
        pdf.set_fill_color(*LGRAY)
        _rounded_rect(pdf,cx, cy, card_w, card_h, 2, '1234', style='F')

        # Label (small, multi-line)
        pdf.set_font('Helvetica', '', 5.5)
        pdf.set_text_color(80, 80, 80)
        pdf.set_xy(cx + 3, cy + 3)
        pdf.multi_cell(card_w - 6, 3.5, label, align='L')

        # Value (large, bold)
        pdf.set_font('Helvetica', 'B', 18)
        pdf.set_text_color(*DARK)
        pdf.set_xy(cx + 3, cy + card_h - 10)
        pdf.cell(card_w - 6, 9, value, align='L', ln=0)

    return y + card_h   # new y position


# ════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/parse', methods=['POST'])
def parse():
    result = {}
    if 'clarity_csv' in request.files:
        content = request.files['clarity_csv'].read().decode('utf-8-sig')
        result['clarity'] = parse_clarity_csv(content)
    if 'meta_csv' in request.files:
        content = request.files['meta_csv'].read().decode('utf-8-sig')
        result['meta'] = parse_meta_csv(content)
    return jsonify(result)


@app.route('/generate', methods=['POST'])
def generate():
    try:
        from fpdf import FPDF  # noqa – verify import works
    except ImportError:
        return jsonify({'error': 'fpdf2 is niet geinstalleerd. Run: pip install fpdf2'}), 500

    try:
        data = request.json

        # Enrich each vacature with formatted values + timeline
        for v in data.get('vacatures', []):
            v['fmt_sessions']    = fmt_number(v.get('sessions', 0))
            v['fmt_impressions'] = fmt_number(v.get('impressions', 0))
            v['fmt_reach']       = fmt_number(v.get('reach', 0))
            v['fmt_scroll']      = fmt_decimal(v.get('scroll_depth', 0)) + '%'
            v['fmt_time']        = f"{v.get('avg_time', 0)} sec"
            v['fmt_start']       = fmt_date(v.get('start_date', ''))
            v['fmt_end']         = fmt_date(v.get('end_date', ''))
            v['timeline']        = timeline_data(v.get('start_date', ''), v.get('end_date', ''))

        pdf_bytes = _make_pdf(data)

        buf   = io.BytesIO(pdf_bytes)
        klant = data.get('klant_naam', 'rapport').replace(' ', '_')
        year  = datetime.now().year

        return send_file(
            buf,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'{year}_-_{klant}_-_Wervingsrapport.pdf',
        )

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


if __name__ == '__main__':
    port  = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
