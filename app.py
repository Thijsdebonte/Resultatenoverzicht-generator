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


def _decode_img(data_url):
    """Decode a base64 data-URL to a BytesIO object."""
    if not data_url or ',' not in data_url:
        return None
    try:
        _, data = data_url.split(',', 1)
        return io.BytesIO(base64.b64decode(data))
    except Exception:
        return None


def _load_static_img(filename):
    """Load a bundled template image from the static/ folder."""
    path = os.path.join(os.path.dirname(__file__), 'static', filename)
    if os.path.exists(path):
        with open(path, 'rb') as f:
            return io.BytesIO(f.read())
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
#  PDF GENERATION — Template overlay approach
#
#  Both cover and data pages use a pre-designed JPG as full-
#  page background; only dynamic values are overlaid as text
#  at fixed coordinates.
#
#  Cover overlay:
#    • Client name  — x=173, y=42  (below "voor", green bold 24)
#
#  Data page overlays  (page 297 × 167 mm):
#    Header:
#      • Job title   — x=36,  y=3.5,  w=189,  green bold 11, centred
#      • Date range  — x=225, y=3,    w=66,   muted 7, centred
#    Section 1 — Clarity (3 equal cards, card_h=24):
#      card_w ≈ 91.67 mm,  value_y = 70.5
#      • Sessies      x= 11,    y=70.5
#      • Gem. tijd    x=105.7,  y=70.5
#      • Scroll       x=200.3,  y=70.5
#    Section 2 — Meta (2 equal cards):
#      card_w = 139 mm,  value_y = 105
#      • Weergaven    x= 11,    y=105
#      • Bereik       x=153,    y=105
#    Section 3 — Sollicitaties (1 card, max_w=55):
#      value_y = 139.5
#      • Sollicitaties x=11, y=139.5
# ════════════════════════════════════════════════════════════

def _make_pdf(data):
    from fpdf import FPDF

    pdf = FPDF(unit='mm', format=(PAGE_W, PAGE_H))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)

    _cover_page(pdf, data)

    # Load the shared data-page template once; reused for every vacature
    data_tpl = _load_static_img('data_template.jpg')
    for v in data.get('vacatures', []):
        _results_page(pdf, v, data_tpl)

    return bytes(pdf.output())


def _cover_page(pdf, data):
    pdf.add_page()

    photo_w = round(PAGE_W * 0.55, 1)   # ≈ 163 mm  (right panel starts here)
    right_x = photo_w
    right_w = PAGE_W - photo_w

    # ── Full-page cover template (bundled static asset) ──────
    cover_tpl = _load_static_img('cover_template.jpg')
    if cover_tpl:
        pdf.image(cover_tpl, x=0, y=0, w=PAGE_W, h=PAGE_H)
    else:
        # Minimal fallback when no template is provided
        pdf.set_fill_color(*DARK)
        pdf.rect(right_x, 0, right_w, PAGE_H, style='F')
        pdf.set_font('Helvetica', 'B', 21)
        pdf.set_text_color(*WHITE)
        pdf.set_xy(right_x + 10, 18)
        pdf.multi_cell(right_w - 14, 11, 'Wervingsrapport\nvoor', align='L')

    # ── Overlay: client name (green bold, below "voor") ──────
    # x aligns with the start of "Wervingsrapport" in the template (~166 mm)
    # y sits just below "voor" which ends at roughly 49 mm
    pdf.set_text_color(*GREEN)
    pdf.set_font('Helvetica', 'B', 28)
    pdf.set_xy(166, 52)
    pdf.multi_cell(right_w - 14, 13, data.get('klant_naam', ''), align='L')


def _results_page(pdf, v, data_template_bytes=None):
    pdf.add_page()

    BX      = 8
    HDR_H   = 12
    cw      = PAGE_W - BX * 2   # 281 mm
    gap     = 3

    # ── Full-page data template ──────────────────────────────
    if data_template_bytes:
        data_template_bytes.seek(0)
        pdf.image(data_template_bytes, x=0, y=0, w=PAGE_W, h=PAGE_H)

    # ── Overlay: function title (header, centred, green) ─────
    title_x = BX + 28          # 36 mm
    title_w = PAGE_W - title_x - 72   # 189 mm
    pdf.set_font('Helvetica', 'B', 11)
    pdf.set_text_color(*GREEN)
    pdf.set_xy(title_x, (HDR_H - 5) / 2)
    pdf.cell(title_w, 5, v.get('titel', ''), align='C', ln=0)

    # ── Overlay: date range (header, right, muted) ───────────
    date_str = f"{v.get('fmt_start', '')} – {v.get('fmt_end', '')}"
    pill_w = 66
    pill_x = PAGE_W - pill_w - 6    # 225 mm
    pill_y = (HDR_H - 6) / 2        # 3 mm
    pdf.set_font('Helvetica', '', 7)
    pdf.set_text_color(*MUTED)
    pdf.set_xy(pill_x, pill_y)
    pdf.cell(pill_w, 6, date_str, align='C', ln=0)

    # ── Helper: overlay a single metric value ────────────────
    def val(x, y, w, text):
        pdf.set_font('Helvetica', 'B', 18)
        pdf.set_text_color(*DARK)
        pdf.set_xy(x, y)
        pdf.cell(w, 9, text, align='L', ln=0)

    # ── Section 1 — Clarity (3 equal cards) ─────────────────
    # card_w = (281 - 3*2) / 3  ≈ 91.67 mm
    # card top-y = 56.5,  value y = 56.5 + 14 = 70.5
    n1   = 3
    cw1  = (cw - gap * (n1 - 1)) / n1   # ≈ 91.67
    vy1  = 56.5 + 14                     # 70.5

    val(BX + 3,                  vy1, cw1 - 6, v.get('fmt_sessions',    '0'))
    val(BX + (cw1 + gap) + 3,    vy1, cw1 - 6, v.get('fmt_time',        '0 sec'))
    val(BX + 2 * (cw1 + gap) + 3, vy1, cw1 - 6, v.get('fmt_scroll',    '0%'))

    # ── Section 2 — Meta (2 equal cards) ────────────────────
    # card_w = (281 - 3) / 2 = 139 mm
    # card top-y = 91,  value y = 91 + 14 = 105
    n2   = 2
    cw2  = (cw - gap * (n2 - 1)) / n2   # 139
    vy2  = 91.0 + 14                     # 105

    val(BX + 3,              vy2, cw2 - 6, v.get('fmt_impressions', '0'))
    val(BX + (cw2 + gap) + 3, vy2, cw2 - 6, v.get('fmt_reach',     '0'))

    # ── Section 3 — Sollicitaties (1 card, max_w=55) ─────────
    # card top-y = 125.5,  value y = 125.5 + 14 = 139.5
    max_cw3 = 55.0
    vy3 = 125.5 + 14   # 139.5

    val(BX + 3, vy3, max_cw3 - 6, str(v.get('sollicitaties', 0)))


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

        # Enrich each vacature with formatted values
        for v in data.get('vacatures', []):
            v['fmt_sessions']    = fmt_number(v.get('sessions', 0))
            v['fmt_impressions'] = fmt_number(v.get('impressions', 0))
            v['fmt_reach']       = fmt_number(v.get('reach', 0))
            v['fmt_scroll']      = fmt_decimal(v.get('scroll_depth', 0)) + '%'
            v['fmt_time']        = f"{v.get('avg_time', 0)} sec"
            v['fmt_start']       = fmt_date(v.get('start_date', ''))
            v['fmt_end']         = fmt_date(v.get('end_date', ''))

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
