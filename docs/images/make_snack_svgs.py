"""docs/why-it-helps.md의 과자 봉지 그림 3장을 만든다.

사용: python3 docs/images/make_snack_svgs.py docs/images
"""
import math, os, sys

OUT = sys.argv[1]
FONT = "-apple-system, 'Apple SD Gothic Neo', 'Malgun Gothic', 'Noto Sans KR', sans-serif"
BG, INK, MUTED, LINE = '#ffffff', '#1f2937', '#6b7280', '#cbd5e1'
SNACK, SNACK_DARK, EMPTY, WARN, GOOD = '#f59e0b', '#b45309', '#fff7ed', '#dc2626', '#16a34a'


def svg(w, h, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'font-family="{FONT}">\n<rect width="{w}" height="{h}" rx="12" fill="{BG}"/>\n{body}\n</svg>\n')


def text(x, y, s, size=14, fill=INK, anchor='middle', weight='normal'):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{s}</text>'


_clip = [0]
def bag(x, y, w, h, grams, label=True, outline=LINE, dash=False):
    """윗부분에 봉합선이 있는 봉지. grams/100 만큼 아래부터 채운다."""
    _clip[0] += 1
    cid = f'c{_clip[0]}'
    fill_h = h * grams / 100
    d = ' stroke-dasharray="6 4"' if dash else ''
    out = [f'<clipPath id="{cid}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8"/></clipPath>',
           f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{EMPTY}"/>',
           f'<rect x="{x}" y="{y + h - fill_h:.1f}" width="{w}" height="{fill_h:.1f}" fill="{SNACK}" clip-path="url(#{cid})"/>',
           f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="none" stroke="{outline}" stroke-width="2"{d}/>',
           f'<line x1="{x + 6}" y1="{y + 10}" x2="{x + w - 6}" y2="{y + 10}" stroke="{outline}" stroke-width="2" stroke-dasharray="3 3"/>']
    if label:
        out.append(text(x + w / 2, y + h + 20, f'{grams}g', 13, MUTED))
    return '\n'.join(out)


def split(g):
    full, rest = divmod(g, 100)
    return [100] * full + ([rest] if rest else [])


# 1) 430g → 5봉지
def fig1():
    w, h = 640, 290
    body = [text(w / 2, 36, '과자 430g, 봉지 하나에 최대 100g', 18, weight='bold')]
    bw, bh, gap, x0, y0 = 84, 130, 28, 48, 70
    for k, g in enumerate(split(430)):
        last = k == 4
        body.append(bag(x0 + k * (bw + gap), y0, bw, bh, g, outline=WARN if last else LINE, dash=last))
    lx = x0 + 4 * (bw + gap) + bw / 2
    body.append(text(lx, y0 - 12, '30g만 담김', 13, WARN, weight='bold'))
    body.append(text(w / 2, h - 30, '30g 때문에 70%가 빈 봉지를 하나 더 써서 5봉지가 됩니다', 15, INK))
    return svg(w, h, '\n'.join(body))


# 2) 무게별 봉지 수
def fig2():
    rows = [(400, '−30g', '4봉지', GOOD), (430, '지금', '5봉지', INK), (500, '+70g', '5봉지 그대로', INK), (510, '+80g', '6봉지', WARN)]
    w, rh = 720, 104
    h = 60 + rh * len(rows) + 10
    body = [text(w / 2, 34, '과자 무게가 바뀌면 봉지 수는?', 18, weight='bold')]
    bw, bh, gap, x0 = 52, 64, 14, 190
    for r, (g, tag, res, color) in enumerate(rows):
        y = 60 + r * rh
        body.append(text(24, y + 30, f'{g}g', 20, INK, 'start', 'bold'))
        body.append(text(24, y + 54, tag, 14, MUTED, 'start'))
        for k, part in enumerate(split(g)):
            body.append(bag(x0 + k * (bw + gap), y + 6, bw, bh, part, label=False,
                            outline=WARN if part < 100 and color == WARN else LINE))
            if part < 100:
                body.append(text(x0 + k * (bw + gap) + bw / 2, y + 40, f'{part}g', 13, WARN if color == WARN else SNACK_DARK, weight='bold'))
        body.append(text(w - 24, y + 44, res, 17, color, 'end', 'bold'))
        if r < len(rows) - 1:
            body.append(f'<line x1="20" y1="{y + rh - 12}" x2="{w - 20}" y2="{y + rh - 12}" stroke="#eef2f7" stroke-width="1"/>')
    return svg(w, h, '\n'.join(body))


# 3) 계단 그래프
def fig3():
    w, h = 720, 380
    left, right, top, bottom = 70, 690, 60, 310
    gmin, gmax, bmin, bmax = 300, 620, 3, 7
    X = lambda g: left + (g - gmin) / (gmax - gmin) * (right - left)
    Y = lambda b: bottom - (b - bmin) / (bmax - bmin) * (bottom - top)
    body = [text(w / 2, 32, '봉지 수는 무게에 비례하지 않고 계단처럼 바뀝니다', 18, weight='bold')]
    for b in range(bmin, bmax + 1):
        body.append(f'<line x1="{left}" y1="{Y(b)}" x2="{right}" y2="{Y(b)}" stroke="#eef2f7"/>')
        body.append(text(left - 12, Y(b) + 5, f'{b}봉지', 12, MUTED, 'end'))
    for g in range(gmin, gmax + 1, 100):
        body.append(text(X(g), bottom + 22, f'{g}g', 12, MUTED))
    # 5봉지 구간(401~500g) 강조
    body.append(f'<rect x="{X(400)}" y="{top}" width="{X(500) - X(400)}" height="{bottom - top}" fill="{SNACK}" opacity="0.10"/>')
    pts = []
    for g0 in range(gmin, gmax, 100):
        b = math.ceil((g0 + 1) / 100)
        pts.append(f'{X(g0):.1f},{Y(b):.1f} {X(min(g0 + 100, gmax)):.1f},{Y(b):.1f}')
    body.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{SNACK_DARK}" stroke-width="3"/>')
    # 지금 430g
    cx, cy = X(430), Y(5)
    body.append(f'<circle cx="{cx}" cy="{cy}" r="7" fill="{SNACK_DARK}"/>')
    body.append(text(cx, cy - 16, '지금 430g', 14, INK, weight='bold'))
    # −30g → 4봉지
    body.append(f'<defs><marker id="ag" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="{GOOD}"/></marker>'
                f'<marker id="am" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="{MUTED}"/></marker></defs>')
    body.append(f'<path d="M{cx - 8},{cy + 4} C{cx - 20},{Y(4.5)} {X(400) + 10},{Y(4.3)} {X(400)},{Y(4) - 6}" fill="none" stroke="{GOOD}" stroke-width="2" marker-end="url(#ag)"/>')
    body.append(f'<circle cx="{X(400)}" cy="{Y(4)}" r="6" fill="{GOOD}"/>')
    body.append(text(X(400) - 10, Y(4) + 26, '30g 줄이면 4봉지', 14, GOOD, 'end', 'bold'))
    # +70g까지 5봉지
    ay = cy + 30
    body.append(f'<line x1="{cx + 4}" y1="{ay}" x2="{X(500) - 4}" y2="{ay}" stroke="{MUTED}" stroke-width="2" marker-end="url(#am)"/>')
    body.append(text((cx + X(500)) / 2, ay + 22, '70g까지 늘려도 5봉지', 14, INK, weight='bold'))
    body.append(text(X(500) + 8, Y(6) - 10, '+80g부터 6봉지', 13, WARN, 'start'))
    body.append(text(w / 2, h - 18, '10g, 20g을 줄이는 노력은 봉지 수를 바꾸지 못합니다. 경계를 넘을 때만 한 봉지가 사라집니다.', 13, MUTED))
    return svg(w, h, '\n'.join(body))


os.makedirs(OUT, exist_ok=True)
for name, f in [('snack-430g.svg', fig1), ('snack-compare.svg', fig2), ('snack-steps.svg', fig3)]:
    open(os.path.join(OUT, name), 'w').write(f())
    print(name)
