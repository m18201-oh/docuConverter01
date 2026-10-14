"""한전 표준시장단가 추출기·게이트가 공유하는 상수.

게이트는 이 모듈의 상수(및 순수 기하 헬퍼)만 쓰고 extract.py / extract_hwp.py
의 함수를 부르지 않는다. 정규식·임계값·단위표를 한곳에 둔다(G02j L7).
"""
from __future__ import annotations

import re

# --- 공종코드 ---
CODE_FIND = re.compile(r"[A-Z]{2}\d{3}\.\d{5}")
CODE_RE = CODE_FIND
CODE_FULL = re.compile(r"^[A-Z]{2}\d{3}\.\d{5}$")
STAR_FIND = re.compile(r"[A-Z]{2}\d{3}\.\d+\*")
STAR_RE = STAR_FIND

# 단가: 천단위 쉼표 정수 또는 순수 정수.
# 소수·범위·단위가 붙은 값(1,234.5 / 1,000～2,000 / 500원)은 이 패턴 밖이며
# 행을 드롭하지 않고 price=null + price_raw 보존(G02j L2, price_unparsed).
PRICE_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$|^\d+$")
LABOR_RE = re.compile(r"^\d+(?:\.\d+)?%$")

# L1: 폐지 반기 표기(‘25상)가 아래 아포스트로피로 인쇄돼도 잡는다.
# U+2018 LEFT SINGLE ‘ / U+0027 APOSTROPHE ' / U+2032 PRIME ′ /
# U+0060 GRAVE ` / U+2019 RIGHT SINGLE ’ / U+FF07 FULLWIDTH ＇
HALF_LABOR_APOS = "\u2018'\u2032`\u2019\uff07"
HALF_LABOR_RE = re.compile(r"^[" + HALF_LABOR_APOS + r"]?\d{2}[상하]")

HALF_FMT_RE = re.compile(r"^20\d{2}H[12]$")

INHERIT_CHARS = set("\"'＂〃“”＇")

HEADER_MAP = {
    "공종코드": "code",
    "공종명칭": "name",
    "공종명": "name",
    "규격": "spec",
    "단위": "unit",
    "단가": "price",
    "노무비율": "labor",
    "비고": "remark",
}
HEADER_NORM = {k.replace(" ", ""): v for k, v in HEADER_MAP.items()}

UNIT_NORM = {"주": "tree", "㎡": "m2", "㎥": "m3", "m²": "m2", "m³": "m3", "톤": "ton", "t": "ton", "Ton": "ton"}

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳ⓛ"
CIRCLED_CHARS = CIRCLED

GROUP_MARK = "■"
BANNER_PREFIX = "대분류"
# L6: 합쳐진 배너 글자 수 제한 없음(대분류 Q, R, S …). major 는 첫 글자.
BANNER_RE = re.compile(r"대분류\s*([A-Z])(?:\s*[,，]\s*[A-Z])*\s*(.*)$")
BANNER_TRAIL_RE = re.compile(r"[·.…⋯]+.*$")
# F1: 띠에 인쇄된 대분류 글자 부분 그대로(공백은 하나로): "A", "Q, R". major 는 그 첫 글자만 둔다.
BANNER_LETTERS_RE = re.compile(r"대분류\s*([A-Z](?:\s*[,，]\s*[A-Z])*)")


def banner_major_raw(src: str) -> str:
    """배너 줄에서 대분류 글자 부분(``Q, R``)을 인쇄된 그대로(공백만 하나로) 돌려준다. 없으면 빈 문자열."""
    m = BANNER_LETTERS_RE.search(src or "")
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


# L10: 1행짜리 작은 표는 헤더/데이터 세로선이 ~19.8pt 로 끊긴다.
# 단가칸 상자 세로선은 ~8.5pt(가로 2단) / ~12pt(세로). 15 아래로 내리면 잡음.
# (G02d 실측; conservation._vlines / extract._page_lines 동일)
VLINE_MIN_LEN_PT = 15.0

# L10: 대분류명 — 글자 사이 공백·목차 점선·쪽번호를 뺀 뒤 한글 2자 이상.
# 24자를 넘으면 표 잔여/다음 줄이 붙은 것으로 보고 버린다. (G02f 실측)
MAJOR_NAME_MAX_LEN = 24

FIGURE_CAPTION_PREFIXES = ("[그림", "[표준도")

# PDF 분야 표지(사이에 반기 문구가 끼지 않은 이어붙임)
FIELD_RE_PDF = re.compile(r"([가-힣]+(?:및[가-힣]+)*)분야자체표준시장단가")
# HWP 분야 줄은 '자체표준시장단가' 가 선택
FIELD_RE_HWP = re.compile(r"([가-힣]+(?:및[가-힣]+)*)분야(?:자체표준시장단가)?")

# G02i2 F4: 실물 렌더로 확인한 PUA 만. 확인 안 된 코드는 넣지 않는다.
PUA_DIGIT_MAP = {
    "\uE034": "1",
    "\uE035": "2",
    "\uE037": "4",
    "\uE038": "5",
    "\uE039": "6",
    "\uE03D": "0",
}
PUA_POINT_MAP = {"\uE053": "."}
PUA_SUPER_MAP = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
}

# 가로 2단 경계 탐색 띠(페이지 폭 비율). 뚜렷한 세로선·낱말 공백이 없으면 정중앙.
LANDSCAPE_GUTTER_BAND = (0.35, 0.65)
# 정중앙과 이보다 가까우면 폴백(기존 4권 대칭 2단 유지).
# 표 내부 세로선·칸 사이 공백은 단 경계가 아니다(실측: 가로 2단 표 세로선이
# 중앙에서 80~100pt 어긋난 위치에 있다). 이보다 크게 치우친 거터만 채택.
LANDSCAPE_GUTTER_CENTER_TOL_PT = 120.0
LANDSCAPE_WORD_GAP_MIN_PT = 18.0

# 본문(표 밖 그룹 제목·【단가정의】 주석) 글자층의 공백 비율.
# 한글이 들어 있는 dict 스팬의 (공백 수 / 글자 수).
# 실측(전권, MalgunGothic 표 글자 포함): 2023H2 0.036, 5권 0.115~0.136.
# 표 밖만: 2023H2 0.023, 5권 0.138~0.150. 한 쪽(p172): 2023H2 0.049, 2024H2 0.141.
# 2023H2 본문은 Type3 라 공백 글리프가 없고, 5권 본문(휴먼명조)은 있다.
# 표 안 MalgunGothic 은 판이 같아 비율을 뒤집지 않는다. 0.07 은 두 군집 사이.
TEXT_LAYER_SPACE_RATIO_MIN = 0.07

# 도식·CAD 삽입 축소 개체의 글자 높이. 본문 한글 ~14pt, 윗첨자 ~5.8pt,
# 2016H1 가시설 도면 안 축소 【단가정의】·표준도 범례 ~1.4–2.5pt (G12 실측).
FIGURE_GLYPH_MAX_H_PT = 5.0


def is_figure_glyph_h(y0: float, y1: float) -> bool:
    """bbox 높이가 축소 도식 글자면 True. 본문·윗첨자는 False."""
    return (y1 - y0) < FIGURE_GLYPH_MAX_H_PT


def hangul_span_space_ratio(texts: list[str]) -> float:
    """한글이 들어 있는 스팬 문자열의 공백 비율. 빈 입력이면 1.0(게이트 꺼짐)."""
    parts = [t for t in texts if t and any("가" <= c <= "힣" for c in t)]
    n = sum(len(t) for t in parts)
    if n == 0:
        return 1.0
    return sum(t.count(" ") for t in parts) / n


# 수량산출 예시도 거리구간(0-150m, 50m~100m, L=70m～100m)
DIST_RANGE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*m\s*[~∼\-～]\s*\d+(?:\.\d+)?\s*m"
    r"|L\s*=\s*\d+(?:\.\d+)?\s*m"
    r"|\d+(?:\.\d+)?\s*[~∼\-～]\s*\d+(?:\.\d+)?\s*m"
)


def cluster_floats(xs: list[float], tol: float) -> list[float]:
    if not xs:
        return []
    xs = sorted(xs)
    groups = [[xs[0]]]
    for x in xs[1:]:
        if x - groups[-1][-1] <= tol:
            groups[-1].append(x)
        else:
            groups.append([x])
    return [sum(g) / len(g) for g in groups]


def landscape_gutter_x(
    width: float,
    v_xs: list[float],
    word_xs: list[float] | None = None,
) -> float:
    """가로 판형 단 경계 x. 세로선·낱말 x 군집, 없으면 정중앙.

    추출 함수가 아니다. extract 와 conservation 이 같은 임계값으로 같은 값을 낸다.
    """
    mid = width / 2.0
    if width <= 0:
        return mid
    lo = width * LANDSCAPE_GUTTER_BAND[0]
    hi = width * LANDSCAPE_GUTTER_BAND[1]
    found: float | None = None
    band_v = [x for x in v_xs if lo <= x <= hi]
    if len(band_v) >= 2:
        clusters = cluster_floats(band_v, 8.0)
        if clusters:
            found = min(clusters, key=lambda x: abs(x - mid))
    if found is None and word_xs:
        xs = sorted(x for x in word_xs if lo <= x <= hi)
        best_gap = 0.0
        best_at: float | None = None
        for i in range(len(xs) - 1):
            gap = xs[i + 1] - xs[i]
            if gap > best_gap:
                best_gap = gap
                best_at = (xs[i] + xs[i + 1]) / 2.0
        if best_at is not None and best_gap >= LANDSCAPE_WORD_GAP_MIN_PT:
            found = best_at
    if found is None:
        return mid
    if abs(found - mid) <= LANDSCAPE_GUTTER_CENTER_TOL_PT:
        return mid
    # 표 칸 세로선은 근처에 낱말이 붙어 있다. 단 경계는 비어 있다.
    if word_xs:
        dens = sum(1 for w in word_xs if abs(w - found) <= 15.0)
        if dens > 3:
            return mid
    return found


# --- F2: 옛 판 보강 (2016H1~2019H2) ---

# 칸 안에서 줄이 꺾여 두 줄로 찍힌 공종코드(2016H1 p112 「ND109.1112」+ 아래 줄 「1」).
CODE_WRAP_HEAD_RE = re.compile(r"^[A-Z]{2}\d{3}\.\d{4}$")


def code_tail_below(
    head: tuple[float, float, float, float],
    tail: tuple[float, float, float, float],
) -> bool:
    """head(코드 앞 열 자리) 바로 아래 줄에 있는 낱글자 tail 이 같은 칸의 코드 꼬리인가.

    순수 기하: tail 가운데 x 가 head 가로 안, tail 윗끝이 head 아랫끝에서 -4~8pt 안,
    글자 높이가 head 와 비슷하다(1.6배 이하).
    """
    hx0, hy0, hx1, hy1 = head
    tx0, ty0, tx1, ty1 = tail
    xc = (tx0 + tx1) / 2.0
    if not (hx0 - 1.0 <= xc <= hx1 + 1.0):
        return False
    if not (-4.0 <= ty0 - hy1 <= 8.0):
        return False
    return (ty1 - ty0) <= 1.6 * (hy1 - hy0)


# 병합 칸: 같은 칸이 두 행 이상에 걸쳐 있으면 그 사이 가로 경계선이 이 칸에서만 끊긴다.
MERGED_CELL_GAP_COVER_MAX = 0.2  # 칸 너비 중 가로선이 덮는 비율이 이 이하면 끊긴 것
MERGED_CELL_ROW_COVER_MIN = 0.5  # 같은 경계에서 표 전체 너비 중 가로선이 덮는 비율이 이 이상이면 행 경계가 있다


def _cover(segs: list[tuple[float, float]], a: float, b: float) -> float:
    """가로선 조각 목록이 [a,b] 를 덮는 길이."""
    if b <= a:
        return 0.0
    spans = sorted((max(s0, a), min(s1, b)) for s0, s1 in segs if min(s1, b) > max(s0, a))
    total = 0.0
    cur_e = a
    for s0, s1 in spans:
        s0 = max(s0, cur_e)
        if s1 > s0:
            total += s1 - s0
            cur_e = s1
    return total


def boundary_splits_cell(
    segs: list[tuple[float, float]],
    cell: tuple[float, float],
    table: tuple[float, float],
) -> bool:
    """한 행 경계(같은 y 의 가로선 조각들)가 cell 열을 가르지 않고 다른 열에는 있는가.

    segs 는 그 경계 y 에 있는 가로선 조각 (x0, x1) 목록, cell 은 칸 x 범위, table 은 표 x 범위.
    표 안쪽 다른 열은 가로선이 있는데 이 칸만 끊겼으면 칸이 아래 행까지 이어진 병합 칸이다.
    """
    cw = cell[1] - cell[0]
    rest = (table[1] - table[0]) - cw
    if cw <= 0 or rest <= 0:
        return False
    in_cell = _cover(segs, cell[0], cell[1])
    in_table = _cover(segs, table[0], table[1])
    others = in_table - in_cell
    return in_cell <= cw * MERGED_CELL_GAP_COVER_MAX and others >= rest * MERGED_CELL_ROW_COVER_MIN


# --- 그림 영역(F2-a) ---
# 그림 제목 줄([표준도]·[그림) 아래에서, 도면 벡터 묶음·이미지 안에 있는 글자는
# 도면 라벨이다(5pt 이상이라 FIGURE_GLYPH_MAX_H_PT 거름에 걸리지 않는다).
FIGURE_REGION_MIN_W_PT = 80.0
FIGURE_REGION_MIN_H_PT = 40.0


def figure_regions(page) -> list[tuple[float, float, float, float]]:
    """쪽의 도면 영역(벡터 묶음·이미지 상자) 목록. 너무 작은 것은 뺀다. page 는 PyMuPDF Page."""
    out: list[tuple[float, float, float, float]] = []
    try:
        clusters = page.cluster_drawings()
    except Exception:
        clusters = []
    for r in clusters:
        if r.width >= FIGURE_REGION_MIN_W_PT and r.height >= FIGURE_REGION_MIN_H_PT:
            out.append((float(r.x0), float(r.y0), float(r.x1), float(r.y1)))
    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    for im in infos:
        b = im.get("bbox")
        if not b or len(b) != 4:
            continue
        if (b[2] - b[0]) >= FIGURE_REGION_MIN_W_PT and (b[3] - b[1]) >= FIGURE_REGION_MIN_H_PT:
            out.append((float(b[0]), float(b[1]), float(b[2]), float(b[3])))
    return out


def in_figure_area(
    xc: float,
    yc: float,
    caption_bottom: float | None,
    regions: list[tuple[float, float, float, float]],
) -> bool:
    """그림 제목 줄 아래(caption_bottom 보다 아래)의 도면 영역 안에 있는 점인가."""
    if caption_bottom is None or yc <= caption_bottom:
        return False
    return any(x0 <= xc <= x1 and y0 <= yc <= y1 for x0, y0, x1, y1 in regions)


# --- 그림 범례·캡션(FA1) ---
# 제목 줄([표준도]·[그림)이 없는 그림도 있다 — 꺾쇠 캡션(<천장점검로 상세도>·<XG-21 규격>)이
# 그림 영역 안에 찍힌 그림이다. 그 영역 안 글자와, 그 글자 줄 바로 아래에 이어진 범례 줄은
# 주석이 아니라 그림에 딸린 글이다(2024H1 p98 JM900.10800 ⑤ 끝 「<XG-21 규격> - LW : 101.6㎜ …」).
FIGURE_BRACKET_CAPTION_RE = re.compile(r"<[^<>]{1,30}>")
FIGURE_LEGEND_GAP_FACTOR = 1.0  # 위 줄과의 세로 틈이 글줄 높이의 이 배수 이하면 이어진 범례 줄
FIGURE_LEGEND_SIZE_FACTOR = 1.1  # 이어 붙는 줄은 위 줄보다 큰 글자(본문 문장)가 아니어야 한다


def bracket_caption_boxes(
    lines: list[list[tuple[tuple[float, float, float, float], str]]],
) -> list[tuple[float, float, float, float]]:
    """글줄 목록([(상자, 글)] 의 목록)에서 꺾쇠 캡션 <…> 하나하나의 상자(x0, y0, x1, y1) 목록."""
    out: list[tuple[float, float, float, float]] = []
    for ln in lines:
        ordered = sorted(ln, key=lambda t: t[0][0])
        pos: list[tuple[int, int, tuple[float, float, float, float]]] = []
        joined = ""
        for box, text in ordered:
            if joined:
                joined += " "
            pos.append((len(joined), len(joined) + len(text), box))
            joined += text
        for m in FIGURE_BRACKET_CAPTION_RE.finditer(joined):
            bs = [b for a, e, b in pos if a < m.end() and e > m.start()]
            if bs:
                out.append(
                    (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))
                )
    return out


def captioned_figure_regions(
    regions: list[tuple[float, float, float, float]],
    captions: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """꺾쇠 캡션이 안에 있거나 바로 아래(캡션 한 줄 높이 이내)에 붙은 도면 영역.

    아래에 붙은 캡션은 영역 아랫끝을 캡션 아랫끝까지 늘려 돌려준다(캡션 글줄이 영역 안으로 들어오게).
    """
    out: list[tuple[float, float, float, float]] = []
    for x0, y0, x1, y1 in regions:
        bottom = y1
        hit = False
        for cx0, cy0, cx1, cy1 in captions:
            xc, yc, h = (cx0 + cx1) / 2.0, (cy0 + cy1) / 2.0, cy1 - cy0
            if not (x0 <= xc <= x1):
                continue
            if y0 <= yc <= y1 or (y1 <= cy0 and cy0 - y1 <= FIGURE_LEGEND_GAP_FACTOR * h):
                hit = True
                bottom = max(bottom, cy1)
        if hit:
            out.append((x0, y0, x1, bottom))
    return out


def figure_attached_flags(
    boxes: list[tuple[float, float, float, float]],
    marked: list[tuple[float, float, float, float]],
) -> list[bool]:
    """각 글 상자가 그림에 딸린 글인가: 꺾쇠 캡션 그림 영역 안이거나, 그런 글 바로 아래에 이어진 줄.

    이어진 줄 = 위에 그림 글이 있고(세로 틈이 글 높이 이하) 가로로 겹치며, 위 글보다 큰 글자가 아닌 것.
    """
    flags = [
        any(r[0] <= (b[0] + b[2]) / 2.0 <= r[2] and r[1] <= (b[1] + b[3]) / 2.0 <= r[3] for r in marked)
        for b in boxes
    ]
    if not any(flags):
        return flags
    order = sorted(range(len(boxes)), key=lambda i: (boxes[i][1], boxes[i][0]))
    changed = True
    while changed:
        changed = False
        for i in order:
            if flags[i]:
                continue
            b = boxes[i]
            h = b[3] - b[1]
            for j in order:
                if not flags[j] or j == i:
                    continue
                f = boxes[j]
                fh = f[3] - f[1]
                if (
                    (f[1] + f[3]) / 2.0 < (b[1] + b[3]) / 2.0
                    and b[1] - f[3] <= FIGURE_LEGEND_GAP_FACTOR * h
                    and b[1] >= f[1]
                    and min(b[2], f[2]) - max(b[0], f[0]) > 0
                    and h <= fh * FIGURE_LEGEND_SIZE_FACTOR
                ):
                    flags[i] = True
                    changed = True
                    break
    return flags


# --- 축소 ■ 제목(F2-e) ---
# 원문 조판 결함: 어떤 그룹의 제목·【단가정의】 ①②가 도면 위 1~4pt 축소 글자로만 찍혀
# 있다(2016H1 p49 AE14*). 축소 ■ 제목은 그 아래 첫 본문 코드가 제목의 코드 접두와
# 같고 그 사이에 (5pt 이상) 실제 ■ 제목이 없을 때만 그룹 경계로 인정한다.
# 그 밖(2016H1 p52 등 도면 안 축소 사본 페이지)은 그룹이 아니다.
TINY_TITLE_RE = re.compile(r"^■\s*([A-Z]{2}\d+)\*")


def accept_tiny_titles(
    titles: list[tuple[float, str]],
    real_header_ys: list[float],
    code_rows: list[tuple[float, str]],
) -> list[float]:
    """받아들일 축소 ■ 제목의 y 목록.

    titles: [(yc, 공백 없는 글줄)], real_header_ys: 같은 단 실제 ■ 줄 y,
    code_rows: [(yc, 코드)] 같은 단 본문 코드 행.
    """
    accepted: dict[float, float] = {}  # 첫 코드 yc -> 제목 yc (같은 코드에 여러 제목이면 마지막)
    for yt, txt in sorted(titles):
        m = TINY_TITLE_RE.match(txt.replace(" ", ""))
        if not m:
            continue
        below = sorted((y, c) for y, c in code_rows if y > yt)
        if not below:
            continue
        yc, code = below[0]
        if not code.startswith(m.group(1)):
            continue
        if any(yt < ry < yc for ry in real_header_ys):
            continue
        accepted[yc] = yt
    return sorted(accepted.values())
