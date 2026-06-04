"""Generate a synthetic demo GIF for the README. No real data."""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import math

W, H = 960, 540
OUT = Path(__file__).resolve().parent.parent / "docs" / "media" / "demo.gif"

BG = (15, 18, 26)
PANEL = (24, 28, 40)
ACCENT = (88, 166, 255)
GREEN = (87, 204, 153)
RED = (239, 99, 99)
YELLOW = (240, 200, 80)
TEXT = (220, 224, 232)
MUTED = (140, 148, 165)


def font(size, bold=False):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default()


def base():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # top bar
    d.rectangle([0, 0, W, 44], fill=(10, 12, 18))
    d.ellipse([14, 14, 30, 30], fill=(255, 95, 86))
    d.ellipse([38, 14, 54, 30], fill=(255, 189, 46))
    d.ellipse([62, 14, 78, 30], fill=(39, 201, 63))
    d.text((W // 2 - 90, 14), "Kutumb · Finance OS", fill=TEXT, font=font(16, True))
    return img, d


def panel(d, x, y, w, h, title):
    d.rectangle([x, y, x + w, y + h], fill=PANEL, outline=(50, 56, 74))
    d.text((x + 14, y + 10), title, fill=MUTED, font=font(13, True))


def frame_loading(progress):
    img, d = base()
    d.text((W // 2 - 130, 200), "Syncing inboxes…", fill=TEXT, font=font(22, True))
    bw = 500
    bx = (W - bw) // 2
    by = 260
    d.rectangle([bx, by, bx + bw, by + 14], fill=PANEL, outline=(50, 56, 74))
    d.rectangle([bx, by, bx + int(bw * progress), by + 14], fill=ACCENT)
    msgs = [
        "Connecting to Gmail (IMAP)…",
        "Connecting to Yahoo (IMAP)…",
        "Parsing HDFC e-Statement…",
        "Parsing Zerodha tradebook…",
        "Parsing CAMS CAS PDF…",
        "Deduping transactions…",
        "Refreshing AMFI NAV file…",
        "Computing XIRR…",
        "Generating recommendations…",
    ]
    idx = min(int(progress * len(msgs)), len(msgs) - 1)
    d.text((bx, by + 30), msgs[idx], fill=MUTED, font=font(13))
    return img


def frame_networth(t):
    img, d = base()
    panel(d, 30, 70, 440, 200, "NET WORTH (FAMILY)")
    val = 1.0 + 0.4 * (math.sin(t * 2) + 1) / 2
    d.text((50, 110), f"₹ {62 + 10 * val:.1f} L", fill=TEXT, font=font(40, True))
    d.text((50, 165), "+ 12.4% YoY", fill=GREEN, font=font(16, True))
    # sparkline
    pts = []
    for i in range(60):
        x = 50 + i * 6.5
        y = 240 - 30 * math.sin(i * 0.18 + t) - i * 0.4
        pts.append((x, y))
    d.line(pts, fill=ACCENT, width=2)

    panel(d, 490, 70, 440, 200, "ASSET ALLOCATION")
    segs = [("Equity MF", 38, GREEN), ("Direct Eq", 18, ACCENT),
            ("PPF/EPF", 22, YELLOW), ("FD/Cash", 14, MUTED), ("Gold/SGB", 8, RED)]
    start = -90
    cx, cy, r = 590, 175, 70
    for name, pct, color in segs:
        end = start + pct * 3.6
        d.pieslice([cx - r, cy - r, cx + r, cy + r], start, end, fill=color)
        start = end
    ly = 95
    for name, pct, color in segs:
        d.rectangle([700, ly, 712, ly + 12], fill=color)
        d.text((720, ly - 2), f"{name}  {pct}%", fill=TEXT, font=font(13))
        ly += 28

    panel(d, 30, 290, 900, 220, "XIRR BY INSTRUMENT CLASS")
    bars = [("Equity MF", 16.2, GREEN), ("Direct Equity", 22.4, GREEN),
            ("Mid+Smallcap", 28.1, GREEN), ("PPF", 7.1, YELLOW),
            ("FD", 6.8, YELLOW), ("Gold/SGB", 11.3, ACCENT)]
    bx = 60
    for name, v, color in bars:
        h = int(v * 4)
        d.rectangle([bx, 470 - h, bx + 90, 470], fill=color)
        d.text((bx, 478), name, fill=MUTED, font=font(11))
        d.text((bx, 460 - h - 18), f"{v}%", fill=TEXT, font=font(13, True))
        bx += 140
    return img


def market_strip(d, y):
    """NSE market signals strip across the top."""
    panel(d, 30, y, 900, 90, "LIVE MARKET SIGNALS · NSE  (12h cached)")
    indices = [
        ("NIFTY 50",      "P/E 22.4", "FAIR",      YELLOW, 24700),
        ("MIDCAP 150",    "P/E 34.8", "EXPENSIVE", RED,    18420),
        ("SMALLCAP 250",  "P/E 31.2", "EXPENSIVE", RED,    16180),
        ("USD/INR",       "₹ 83.42",  "STABLE",    GREEN,  None),
    ]
    x = 50
    for name, pe, zone, color, level in indices:
        d.text((x, y + 32), name, fill=MUTED, font=font(11, True))
        d.text((x, y + 48), pe, fill=TEXT, font=font(16, True))
        d.rectangle([x, y + 70, x + 70, y + 86], fill=color)
        d.text((x + 6, y + 71), zone, fill=(10, 10, 10), font=font(11, True))
        x += 220


def frame_recs_market():
    """Frame A: market signals + headline."""
    img, d = base()
    d.text((30, 56), "Market-Aware Recommendations", fill=TEXT, font=font(22, True))
    d.text((30, 84), "NSE live P/E zones · personal cashflow · loan amortisation · goal gaps",
           fill=MUTED, font=font(12))
    market_strip(d, 110)

    # Headline reasoning block
    panel(d, 30, 220, 900, 290, "ENGINE REASONING (this month)")
    bullets = [
        ("•", "Midcap 150 P/E 34.8  →  EXPENSIVE zone (>1σ above 10yr median)", RED),
        ("•", "Your portfolio: 28% midcap + 14% smallcap  =  42% high-beta exposure",  YELLOW),
        ("•", "Home-loan principal FY: ₹ 1.62 L  ×  your 60% share  =  ₹ 97k toward 80C", ACCENT),
        ("•", "EPF FY contribution: ₹ 72k  →  80C already at ₹ 1.69 L (cap ₹ 1.5 L)", ACCENT),
        ("•", "Emergency fund: 1.8 months  vs  target 6  →  ₹ 3.8 L gap",            RED),
        ("•", "Goals: child-edu corpus on track  ·  retirement 18% behind glide-path", YELLOW),
        ("•", "Spouse PPF: ₹ 70k deposited / ₹ 1.5 L cap  →  ₹ 80k pending by Mar-31", GREEN),
    ]
    yy = 258
    for mark, line, color in bullets:
        d.text((50, yy), mark, fill=color, font=font(15, True))
        d.text((70, yy), line, fill=TEXT, font=font(13))
        yy += 32
    return img


def frame_recs_cards():
    """Frame B: detailed personalised rec cards."""
    img, d = base()
    d.text((30, 56), "Personalised Action Plan", fill=TEXT, font=font(22, True))
    d.text((30, 84), "4 recommendations · estimated ₹ 47.5k/month redeployed",
           fill=MUTED, font=font(12))

    recs = [
        ("HIGH", RED,
         "Emergency Fund Gap",
         "1.8 mo runway vs 6 mo target  ·  shortfall ₹ 3.8 L",
         "→ Start ₹ 25k/mo SIP into ICICI Liquid until Aug-2026  ·  freed by stopping ELSS"),
        ("HIGH", RED,
         "Midcap / Smallcap Overweight",
         "42% high-beta  ·  Midcap150 P/E EXPENSIVE  ·  Smallcap250 P/E EXPENSIVE",
         "→ Pause ₹ 15k/mo midcap SIP + ₹ 7.5k/mo smallcap SIP  ·  redirect to Nifty 50 index"),
        ("MED",  YELLOW,
         "ELSS 80C Dead-Weight",
         "80C already at ₹ 1.69 L via home-loan principal + EPF (cap ₹ 1.5 L)",
         "→ Stop ₹ 12.5k/mo ELSS SIP  ·  saves ₹ 0 tax  ·  redirect to emergency fund"),
        ("LOW",  GREEN,
         "Spouse PPF Top-up",
         "₹ 70k deposited / ₹ 1.5 L FY cap  ·  deadline Mar-31-2026",
         "→ Transfer ₹ 80k to Union Bank PPF before Mar-31  ·  locks 7.1% tax-free"),
    ]
    y = 118
    for tag, color, title, why, action in recs:
        d.rectangle([30, y, 930, y + 92], fill=PANEL, outline=(50, 56, 74))
        d.rectangle([30, y, 36, y + 92], fill=color)
        d.rectangle([50, y + 12, 100, y + 30], fill=color)
        d.text((58, y + 13), tag, fill=(10, 10, 10), font=font(12, True))
        d.text((115, y + 10), title, fill=TEXT, font=font(16, True))
        d.text((115, y + 36), why, fill=MUTED, font=font(12))
        d.text((115, y + 60), action, fill=ACCENT, font=font(13, True))
        y += 102
    return img


def frame_recs_memory():
    """Frame C: persistent memory diff vs last month."""
    img, d = base()
    d.text((30, 56), "Recommendation Memory", fill=TEXT, font=font(22, True))
    d.text((30, 84), "Diff vs last month  ·  multi-month progress tracking",
           fill=MUTED, font=font(12))

    # Three columns: New / Resolved / Persisting
    cols = [
        ("NEW (this month)",  RED, [
            ("Midcap overweight", "triggered by NSE P/E zone flip Apr→May"),
            ("Emergency fund gap", "salary irregularity widened gap to 3.8 L"),
        ]),
        ("RESOLVED ✓",        GREEN, [
            ("Direct equity scatter", "12 → 8 holdings (consolidated Apr)"),
            ("International gap",     "added ₹ 8k/mo Motilal Nasdaq-100 FoF"),
        ]),
        ("PERSISTING",        YELLOW, [
            ("ELSS 80C dead-weight",  "5 months running · est. tax lost ₹ 0"),
            ("Spouse PPF top-up",     "3 months running · 23 days to deadline"),
        ]),
    ]
    cw = 290
    cx = 30
    for header, color, items in cols:
        d.rectangle([cx, 118, cx + cw, 500], fill=PANEL, outline=(50, 56, 74))
        d.rectangle([cx, 118, cx + cw, 142], fill=color)
        d.text((cx + 12, 122), header, fill=(10, 10, 10), font=font(13, True))
        yy = 158
        for title, sub in items:
            d.text((cx + 12, yy), title, fill=TEXT, font=font(14, True))
            d.text((cx + 12, yy + 22), sub, fill=MUTED, font=font(11))
            yy += 60
        cx += cw + 15

    # Trend arrows footer
    panel(d, 30, 510, 900, 25, "")
    return img


def frame_grafana(t):
    img, d = base()
    d.text((30, 60), "Grafana — Cash Flow", fill=TEXT, font=font(22, True))
    panel(d, 30, 100, 900, 380, "MONTHLY INCOME VS EXPENSES")
    months = ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May"]
    base_y = 440
    for i, m in enumerate(months):
        x = 80 + i * 100
        inc = 90 + 20 * math.sin(i + t)
        exp = 60 + 15 * math.cos(i * 1.2 + t)
        d.rectangle([x, base_y - inc, x + 30, base_y], fill=GREEN)
        d.rectangle([x + 36, base_y - exp, x + 66, base_y], fill=RED)
        d.text((x + 10, base_y + 6), m, fill=MUTED, font=font(12))
    d.rectangle([750, 130, 762, 142], fill=GREEN)
    d.text((770, 128), "Income", fill=TEXT, font=font(12))
    d.rectangle([750, 150, 762, 162], fill=RED)
    d.text((770, 148), "Expenses", fill=TEXT, font=font(12))
    return img


frames = []
# Loading
for i in range(12):
    frames.append((frame_loading(i / 11), 200))
# Net worth (animated)
for i in range(10):
    frames.append((frame_networth(i * 0.3), 180))
# Hold
frames.append((frame_networth(3.0), 1200))
# Recs — 3-frame story: market signals → action plan → memory diff
frames.append((frame_recs_market(), 3200))
frames.append((frame_recs_cards(), 3500))
frames.append((frame_recs_memory(), 3000))
# Grafana
for i in range(8):
    frames.append((frame_grafana(i * 0.4), 200))
frames.append((frame_grafana(3.2), 1500))

imgs = [f for f, _ in frames]
durs = [d for _, d in frames]

OUT.parent.mkdir(parents=True, exist_ok=True)
imgs[0].save(OUT, save_all=True, append_images=imgs[1:], duration=durs, loop=0, optimize=True)
print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(imgs)} frames)")
