"""표 본문 낱말 보존 검사.

추출 코드(extract.py)의 함수를 재사용하지 않는다.
낱말은 page.get_text("words") 원자료에서 직접 모은다.
"""
from __future__ import annotations

import bisect
import difflib
import logging
import re
from pathlib import Path
from typing import Any

import pymupdf as fitz

_LOG = logging.getLogger(__name__)

CIRCLED_CHARS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳ⓛ"  # G02i2 F1: ⓛ(U+24DB) 도 ①과 같은 항목 기호
CODE_RE = re.compile(r"[A-Z]{2}\d{3}\.\d{5}")
CODE_FULL = re.compile(r"^[A-Z]{2}\d{3}\.\d{5}$")
STAR_RE = re.compile(r"[A-Z]{2}\d{3}\.\d+\*")
PRICE_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$|^\d+$")
LABOR_RE = re.compile(r"^\d+(?:\.\d+)?%$")
HALF_LABOR_RE = re.compile(r"^[‘'′`]?\d{2}[상하]")
INHERIT_CHARS = set('"\'＂〃“”＇')
UNIT_NORM = {"주": "tree", "㎡": "m2", "㎥": "m3", "톤": "ton"}
HEADER_NORM = {
    "공종코드": "code",
    "공종명칭": "name",
    "공종명": "name",
    "규격": "spec",
    "단위": "unit",
    "단가": "price",
    "노무비율": "labor",
    "비고": "remark",
}
RAW_FIELDS = (
    "code",
    "name_raw",
    "spec_raw",
    "unit_raw",
    "price_raw",
    "labor_raw",
    "remark_raw",
    "abolished_at",
)


def _halves(page: fitz.Page) -> list[tuple[str, float, float]]:
    r = page.rect
    if r.width > r.height:
        mid = r.width / 2
        return [("L", 0.0, mid), ("R", mid, r.width)]
    return [("C", 0.0, r.width)]


def _cluster(vals: list[float], tol: float) -> list[float]:
    if not vals:
        return []
    vals = sorted(vals)
    groups = [[vals[0]]]
    for v in vals[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _wide_h(page: fitz.Page, drawings: list | None = None) -> list[tuple[float, float, float]]:
    """(x0, x1, y) 가로 벡터 선. 짧은 선은 버린다."""
    out: list[tuple[float, float, float]] = []
    for d in (drawings if drawings is not None else page.get_drawings()):
        for item in d.get("items", []):
            if item[0] != "l":
                continue
            p1, p2 = item[1], item[2]
            if abs(p1.y - p2.y) >= 0.8:
                continue
            x0, x1 = sorted((float(p1.x), float(p2.x)))
            if x1 - x0 > 40:
                out.append((x0, x1, float((p1.y + p2.y) / 2)))
    return out


def _vlines(page: fitz.Page, drawings: list | None = None) -> list[tuple[float, float, float]]:
    """(x, y0, y1) 세로 벡터 선. 1행 표 ~19.8pt 도 포함한다."""
    out: list[tuple[float, float, float]] = []
    for d in (drawings if drawings is not None else page.get_drawings()):
        for item in d.get("items", []):
            if item[0] != "l":
                continue
            p1, p2 = item[1], item[2]
            if abs(p1.x - p2.x) >= 0.8:
                continue
            y0, y1 = sorted((float(p1.y), float(p2.y)))
            if y1 - y0 > 15:
                out.append((float((p1.x + p2.x) / 2), y0, y1))
    return out


def _vline_splits_word(
    vlines: list[tuple[float, float, float]],
    wx0: float,
    wy0: float,
    wx1: float,
    wy1: float,
) -> bool:
    for vx, vy0, vy1 in vlines:
        if not (wx0 + 0.35 < vx < wx1 - 0.35):
            continue
        if min(wy1, vy1) - max(wy0, vy0) > 1.0:
            return True
    return False


def _bbox_overlap(a: object, b: object, tol: float = 0.5) -> bool:
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        return False
    if len(a) != 4 or len(b) != 4:
        return False
    ax0, ay0, ax1, ay1 = (float(v) for v in a)
    bx0, by0, bx1, by1 = (float(v) for v in b)
    return (min(ax1, bx1) - max(ax0, bx0) > -tol) and (min(ay1, by1) - max(ay0, by0) > -tol)


def _header_field(text: str) -> str | None:
    key = text.strip().replace(" ", "")
    val = HEADER_NORM.get(key)
    if val is not None:
        return val
    # 칸이 커닝 때문에 옆 칸과 들러붙는 경우("단위단가"처럼 두 헤더가 한
    # 낱말로 뭉침, review_round1 N8-scope-suspect 실측: 2025H1 p103 "JG******"
    # 줄바꿈 머리글 표). 뒤쪽 헤더 낱말로 끝나면 그 칸으로 본다 — "단가" 열이
    # 있어야 _header_ys() 가 이 줄을 진짜 표 머리글로 인정하므로 중요하다.
    for hw, field in HEADER_NORM.items():
        if len(hw) >= 2 and key.endswith(hw):
            return field
    return None


def _is_price_tok(t: str) -> bool:
    s = t.strip().replace(" ", "")
    if "폐지" in s:
        return True
    if s == "폐기":
        return True
    if LABOR_RE.match(s):
        return True
    if HALF_LABOR_RE.match(s):
        return True
    if PRICE_RE.match(s) and ("," in s or len(s) >= 4):
        return True
    return False


# G02i2 F4/F5: extract.py _fix_pua_spans 와 같은 대응표·윗첨자 판정을 이 파일
# 안에서 독립적으로 다시 적용한다(이 파일은 extract.py 함수를 재사용하지 않는다
# — 파일 맨 위 원칙). page.get_text("words") 는 스팬 경계 없이 공백만으로
# 낱말을 묶어, PUA 수식(정상 크기+윗첨자 두 스팬)이 한 낱말로 뭉친다 — 그러면
# 낱말 단위 치환으로는 어느 글자가 윗첨자인지 알 수 없어, 글자(rawdict) 단위로
# 먼저 교정한 뒤 그 글자들로 낱말 텍스트를 다시 짠다.
_PUA_DIGIT_MAP = {
    "\uE034": "1", "\uE035": "2", "\uE037": "4", "\uE038": "5", "\uE039": "6", "\uE03D": "0",
}  # 코치 핫픽스 09-17: extract.py 와 같은 키 집합 유지(실물 확인분만)
_PUA_POINT_MAP = {"\uE053": "."}
_PUA_SUPER_MAP = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
}


# extract.py 주석 실측(정상 8.52pt/윗첨자 5.76pt, 정상 12.0pt/윗첨자 8.16pt).
# 크기 비율 0.85·y 6pt 군집은 extract.py 와 같은 판정의 동어반복이라 쓰지 않는다.
_PUA_SUPER_SIZES = (5.76, 8.16)
_PUA_SIZE_TOL = 0.05


def _pua_is_superscript(sz: float, flags: int) -> bool:
    """rawdict 스팬 flags 의 윗첨자 비트(MuPDF bit 0) 또는 실측 윗첨자 크기 정확 일치."""
    if flags & 1:
        return True
    if sz <= 0:
        return False
    return any(abs(sz - super_sz) <= _PUA_SIZE_TOL for super_sz in _PUA_SUPER_SIZES)


def _pua_corrected_chars(page: fitz.Page) -> list[tuple[float, float, float, float, str]]:
    """이 쪽에서 대응표에 있는 PUA 글자만, 윗첨자면 윗첨자 숫자로 바꿔
    (bbox, 교정 글자) 목록으로 돌려준다."""
    raw: list[tuple[float, float, float, float, float, str, int]] = []
    rd = page.get_text("rawdict")
    for b in rd.get("blocks", []):
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                sz = float(s.get("size") or 0.0)
                flags = int(s.get("flags") or 0)
                for c in s.get("chars") or []:
                    ch = c.get("c") or ""
                    if ch in _PUA_DIGIT_MAP or ch in _PUA_POINT_MAP:
                        x0, y0, x1, y1 = c["bbox"]
                        raw.append((float(x0), float(y0), float(x1), float(y1), sz, ch, flags))
    if not raw:
        return []
    out: list[tuple[float, float, float, float, str]] = []
    for x0, y0, x1, y1, sz, ch, flags in raw:
        is_super = _pua_is_superscript(sz, flags)
        if ch in _PUA_DIGIT_MAP:
            d = _PUA_DIGIT_MAP[ch]
            out.append((x0, y0, x1, y1, _PUA_SUPER_MAP[d] if is_super else d))
        else:
            out.append((x0, y0, x1, y1, _PUA_POINT_MAP[ch]))
    return out


def _words(page: fitz.Page) -> list[tuple[float, float, float, float, str]]:
    rows = []
    for w in page.get_text("words"):
        t = w[4]
        if t is None or t == "":
            continue
        rows.append((float(w[0]), float(w[1]), float(w[2]), float(w[3]), t))
    if any("" <= ch <= "" for _x0, _y0, _x1, _y1, t in rows for ch in t):
        corrected = _pua_corrected_chars(page)
        if corrected:
            fixed_rows = []
            for x0, y0, x1, y1, t in rows:
                if not any("" <= ch <= "" for ch in t):
                    fixed_rows.append((x0, y0, x1, y1, t))
                    continue
                in_word = [
                    (cx0, cch)
                    for cx0, cy0, cx1, cy1, cch in corrected
                    if x0 - 0.5 <= (cx0 + cx1) / 2 <= x1 + 0.5
                    and y0 - 0.5 <= (cy0 + cy1) / 2 <= y1 + 0.5
                ]
                if len(in_word) < sum(1 for ch in t if "" <= ch <= ""):
                    fixed_rows.append((x0, y0, x1, y1, t))  # 대응표에 없는 글자 섞임 — 원문 그대로
                    continue
                in_word.sort(key=lambda p: p[0])
                new_t = []
                ci = 0
                for ch in t:
                    if "" <= ch <= "":
                        new_t.append(in_word[ci][1])
                        ci += 1
                    else:
                        new_t.append(ch)
                fixed_rows.append((x0, y0, x1, y1, "".join(new_t)))
            rows = fixed_rows
    return rows


def _header_ys(
    words: list[tuple[float, float, float, float, str]],
    x0: float,
    x1: float,
) -> list[float]:
    cands = []
    for wx0, wy0, wx1, wy1, t in words:
        xc = (wx0 + wx1) / 2
        if not (x0 <= xc < x1):
            continue
        if _header_field(t) is None:
            continue
        cands.append(((wy0 + wy1) / 2, _header_field(t), xc))
    cands.sort(key=lambda c: c[0])
    rows: list[list[tuple[float, str, float]]] = []
    for item in cands:
        if not rows or abs(item[0] - rows[-1][0][0]) > 8:
            rows.append([item])
        else:
            rows[-1].append(item)
    out = []
    for row in rows:
        fields = {f for _, f, _ in row}
        if len(fields) >= 3 and "price" in fields:
            out.append(sum(r[0] for r in row) / len(row))
    return out


def _rec_codes(
    words: list[tuple[float, float, float, float, str]],
    x0: float,
    x1: float,
    table_x0: float | None = None,
    table_x1: float | None = None,
) -> list[tuple[str, float, float, float]]:
    """(code, yc, y0, y1) 본문 코드만(오른쪽에 단가/폐지/노무비율). 표 실제 x 사용."""
    tx0 = x0 if table_x0 is None else table_x0
    tx1 = x1 if table_x1 is None else table_x1
    half_w = tx1 - tx0
    codes = []
    for wx0, wy0, wx1, wy1, t in words:
        if not (tx0 - 2 <= wx0 < tx1):
            continue
        m = CODE_RE.search(t)
        if not m or m.start() > 2:
            continue
        if wx0 > tx0 + half_w * 0.28:
            continue
        yc = (wy0 + wy1) / 2
        thresh = tx0 + half_w * 0.60
        priced = False
        for sx0, sy0, sx1, sy1, st in words:
            sxc = (sx0 + sx1) / 2
            syc = (sy0 + sy1) / 2
            if sxc < thresh or sxc > tx1 + 2:
                continue
            if abs(syc - yc) > 10:
                continue
            if _is_price_tok(st):
                priced = True
                break
        if priced:
            codes.append((m.group(), yc, wy0, wy1))
    codes.sort(key=lambda c: c[1])
    return codes


def _group_ys(
    words: list[tuple[float, float, float, float, str]],
    x0: float,
    x1: float,
) -> list[float]:
    ys = []
    for wx0, wy0, wx1, wy1, t in words:
        xc = (wx0 + wx1) / 2
        if x0 <= xc < x1 and "■" in t:
            ys.append((wy0 + wy1) / 2)
    return _cluster(ys, 8.0)


def _danga_ys(
    words: list[tuple[float, float, float, float, str]],
    x0: float,
    x1: float,
) -> list[float]:
    ys = []
    for wx0, wy0, wx1, wy1, t in words:
        xc = (wx0 + wx1) / 2
        if x0 <= xc < x1 and "단가정의" in t.replace(" ", ""):
            ys.append((wy0 + wy1) / 2)
    return ys


def _table_x(
    hlines: list[tuple[float, float, float]],
    y0: float,
    y1: float,
    hx0: float,
    hx1: float,
    min_w: float,
) -> tuple[float, float]:
    wide = [
        h
        for h in hlines
        if y0 - 30 <= h[2] <= y1 + 30
        and h[0] < hx1 - 20
        and h[1] > hx0 + 20
        and (h[1] - h[0]) >= min_w * 0.8
    ]
    if not wide:
        return hx0 + 8, hx1 - 8
    tx0 = max(min(h[0] for h in wide), hx0 + 2)
    tx1 = min(max(h[1] for h in wide), hx1 - 2)
    return tx0, tx1


def _table_bodies(
    page: fitz.Page,
    words: list[tuple[float, float, float, float, str]] | None = None,
    hlines: list[tuple[float, float, float]] | None = None,
) -> list[tuple[float, float, float, float]]:
    """본문 영역 사각형 (x0,y0,x1,y1). 헤더 행 아래 ~ 표 바닥."""
    if words is None:
        words = _words(page)
    if hlines is None:
        hlines = _wide_h(page)
    layout_minw = page.rect.width * (0.35 if page.rect.width > page.rect.height else 0.45)
    bodies: list[tuple[float, float, float, float]] = []
    for _ph, hx0, hx1 in _halves(page):
        ww = [w for w in words if hx0 - 1 <= (w[0] + w[2]) / 2 < hx1 + 1]
        headers = _header_ys(ww, hx0, hx1)
        tx0, tx1 = _table_x(hlines, 0, page.rect.height, hx0, hx1, layout_minw)
        # 격자 가로선이 반 폭 폴백이면(색인) 표로 보지 않음
        has_grid = abs(tx0 - (hx0 + 8)) > 2 or abs(tx1 - (hx1 - 8)) > 2
        if not headers and not has_grid:
            continue
        codes = _rec_codes(ww, hx0, hx1, table_x0=tx0, table_x1=tx1)
        gys = _group_ys(ww, hx0, hx1)
        dys = _danga_ys(ww, hx0, hx1)
        if not codes:
            continue
        clusters: list[list[tuple[str, float, float, float]]] = []
        cur: list[tuple[str, float, float, float]] = []
        for c in codes:
            if not cur:
                cur = [c]
                continue
            prev = cur[-1][1]
            yc = c[1]
            between_g = any(prev + 4 < g < yc - 4 for g in gys)
            between_h = any(prev + 4 < h < yc - 12 for h in headers)
            if between_g or between_h or (yc - prev) > 95:
                clusters.append(cur)
                cur = [c]
            else:
                cur.append(c)
        if cur:
            clusters.append(cur)

        for cl in clusters:
            y_min = cl[0][1]
            y_max = cl[-1][1]
            header_yc = None
            for hyc in headers:
                if hyc < y_min - 2 and y_min - hyc < 90:
                    header_yc = hyc
            tx0, tx1 = _table_x(hlines, y_min - 40, y_max + 40, hx0, hx1, layout_minw)
            table_w = tx1 - tx0
            hys = _cluster(
                [
                    h[2]
                    for h in hlines
                    if max(h[0], tx0) < min(h[1], tx1)
                    and (min(h[1], tx1) - max(h[0], tx0)) >= table_w * 0.55
                ],
                1.6,
            )
            if header_yc is not None:
                below_h = [hy for hy in hys if header_yc <= hy < y_min - 1]
                y_top = max(below_h) if below_h else header_yc + 10
            else:
                above = [hy for hy in hys if y_min - 30 < hy < y_min - 1]
                y_top = min(above) if above else cl[0][2] - 8
            next_stop = page.rect.height - 36
            later = [h for h in headers if h > y_max + 4]
            later += [g for g in gys if g > y_max + 4]
            later += [d for d in dys if d > y_max + 2]
            if later:
                next_stop = min(next_stop, min(later))
            below = [hy for hy in hys if y_max + 1 < hy < min(next_stop - 1, y_max + 55)]
            y_bot = min(below) if below else cl[-1][3] + 10
            if y_bot <= y_top + 4:
                continue
            bodies.append((tx0, y_top, tx1, y_bot))
    return bodies


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _word_in_text(word: str, field: str, *, allow_digit: bool = True) -> bool:
    if not field:
        return False
    if word in field.split():
        return True
    wn = _compact(word)
    fn = _compact(field)
    if wn and wn in fn:
        if wn.isdigit() and len(wn) <= 2:
            toks = re.findall(r"[0-9]+(?:,[0-9]{3})*|[^\s]+", field)
            return any(_compact(t) == wn or t.replace(",", "") == wn for t in toks)
        return True
    # 붙은 숫자(1,361263 vs 1,361,263): 숫자만 비교
    if allow_digit:
        wd = re.sub(r"[^\d]", "", word)
        if len(wd) >= 4:
            fd = re.sub(r"[^\d]", "", field)
            if wd in fd:
                return True
    return False


def _wholly_in_one_field(word: str, rec: dict) -> bool:
    """공백 무시 부분 문자열로 한 필드에 통째로 들어 있는가. 숫자만 비교는 쓰지 않는다.

    천 단위 콤마만 다른 숫자(1,361263 vs 1,361,263)는 같은 필드로 본다.
    """
    wn = _compact(word)
    if len(wn) < 2:
        return True
    wn_num = wn.replace(",", "")
    word_is_num = bool(re.fullmatch(r"[\d,]+", wn))
    for k in RAW_FIELDS:
        v = rec.get(k)
        if v is None:
            continue
        fn = _compact(str(v))
        if wn in fn:
            return True
        if word_is_num and wn_num and wn_num in fn.replace(",", ""):
            return True
    return False


def _record_blob(rec: dict) -> str:
    parts = []
    for k in RAW_FIELDS:
        v = rec.get(k)
        if v is None:
            continue
        parts.append(str(v))
    return "\n".join(parts)


def _assign(
    word: tuple[float, float, float, float, str],
    records: list[dict],
    subheaders: list[dict],
    *,
    allow_digit: bool = True,
) -> list[str]:
    """낱말이 들어간 레코드 키(또는 소제목 패턴) 목록."""
    x0, y0, x1, y1, text = word
    xc, yc = (x0 + x1) / 2, (y0 + y1) / 2
    hits: list[str] = []
    for rec in records:
        bbox = rec.get("bbox") or [0, 0, 0, 0]
        if len(bbox) != 4:
            continue
        bx0, by0, bx1, by1 = (float(v) for v in bbox)
        if not (by0 - 0.4 <= yc < by1 + 0.4 and bx0 - 1.0 <= xc <= bx1 + 1.0):
            continue
        if _word_in_text(text, _record_blob(rec), allow_digit=allow_digit):
            hits.append(rec.get("key") or rec.get("code") or "?")
    if hits:
        return hits
    for sub in subheaders:
        blob = f"{sub.get('code_pattern') or ''}\n{sub.get('text') or ''}"
        if _word_in_text(text, blob, allow_digit=allow_digit):
            return [f"sub:{sub.get('code_pattern')}"]
    return []


def _skip_word(
    text: str,
    yc: float,
    header_ys: list[float],
    page_h: float,
) -> bool:
    on_header_row = any(abs(yc - hy) <= 9 for hy in header_ys)
    if on_header_row and _header_field(text) is not None:
        return True
    if on_header_row:
        # "노무비율"+"비고" 두 칸이 커닝 때문에 한 낱말로 들러붙는 경우
        # ("노무비율비" + 남은 "고")가 있다 — 정확히 일치하는 헤더 낱말만 보는
        # _header_field() 로는 안 걸러져 게이트가 표 머리글 자체를 주석
        # 낱말로 오인했다(review_round1 실측: 4권 전체 14~15곳).
        # G02i2 F5: 위 "남은 고" 사례 자체가 오히려 안 걸렸다 — "비"·"고"가
        # 아예 두 낱말로 갈라지면(자간 탓) "고"는 "고"로 시작하는 헤더 낱말도,
        # "고"로 시작하는 낱말의 접두도 아니라 startswith 두 방향 다 안 걸린다.
        # "비고"가 "고"로 끝나는 쪽(h.endswith(compact))도 봐야 이 남은 글자를
        # 잡는다(notes_order_mismatch 실측: 4권 전체 수백 곳, 항상 다음 그룹의
        # 첫 항목 "①" 바로 앞에 이 표 머리글의 "고" 가 새어 붙었다).
        compact = text.strip().replace(" ", "")
        if compact and any(h.startswith(compact) or h.endswith(compact) for h in HEADER_NORM):
            return True
        # G02i2 F5 gate_false_pass 보강(코치 독립 시험 T05/gate_false_pass
        # code_issues severity=medium): "공종명"이 커닝 때문에 "공"·"종"·"명"
        # 세 낱말로 완전히 갈라지는 표(2024H1 p130~133 계열 등)에서, 끝쪽
        # 글자("공"·"명")는 위 startswith/endswith 로 걸리지만 가운데 글자
        # ("종")는 그 헤더 낱말의 접두도 접미도 아니라 안 걸려 다음 그룹의
        # 첫 항목 "①" 바로 앞에 새어 붙었다(notes_order_mismatch 실측: 4권
        # 전체 가장 큰 잔여 원인). 진짜 표 머리글 행(on_header_row) 위에서만,
        # 그것도 낱말이 1~2 글자로 아주 짧을 때만 "낱말이 헤더 낱말 안에
        # 부분문자열로 있는지"까지 본다 — 길이를 짧게 제한해 우연히 헤더
        # 낱말 일부와 같은 짧은 조사·어미(실제 주석 문장 낱말)까지 넓게
        # 걸러내는 위험을 줄인다.
        if compact and len(compact) <= 2 and any(compact in h for h in HEADER_NORM):
            return True
        # G02i2 F5: compact.startswith(h) 방향(낱말이 헤더 낱말로 "시작"하는
        # 경우)은 "단가"·"단위" 처럼 흔한 헤더 낱말이 그대로 문장 첫머리에도
        # 나와("①이 단가는…") 헤더 줄과 우연히 y 가 겹치면("단가는"·"단가에는")
        # 실제 항목 문장 첫머리까지 통째로 걸러졌다(notes_order_mismatch 실측:
        # 2024H1~2025H1 전체에서 가장 큰 잔여 원인). 진짜 커닝 사례("노무비율"+
        # "비고"→"노무비율비")는 남는 조각("비")이 다른 헤더 낱말의 접두이기도
        # 하다는 것으로 구분한다 — 조사·서술 남는 조각은 그 조건을 만족하지
        # 않는다.
        for h in HEADER_NORM:
            if not compact.startswith(h):
                continue
            rest = compact[len(h):]
            if rest and any(h2.startswith(rest) for h2 in HEADER_NORM if h2 != h):
                return True
    t = text.strip()
    if t in {"■"}:
        return True
    if yc > page_h * 0.92:
        return True
    return False


def _vline_between(
    vlines: list[tuple[float, float, float]],
    x_left: float,
    x_right: float,
    y0: float,
    y1: float,
) -> bool:
    if x_right - x_left <= 0.7:
        return False
    for vx, vy0, vy1 in vlines:
        if not (x_left + 0.35 < vx < x_right - 0.35):
            continue
        if min(y1, vy1) - max(y0, vy0) > 1.0:
            return True
    return False


def _cluster_word_lines(
    words: list[tuple[float, float, float, float, str]],
    gap: float = 4.0,
) -> list[list[tuple[float, float, float, float, str]]]:
    if not words:
        return []
    words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2.0, w[0]))
    lines: list[list[tuple[float, float, float, float, str]]] = []
    for w in words:
        yc = (w[1] + w[3]) / 2.0
        if not lines:
            lines.append([w])
            continue
        prev_yc = sum((x[1] + x[3]) / 2.0 for x in lines[-1]) / len(lines[-1])
        if abs(yc - prev_yc) <= max(gap, (w[3] - w[1]) * 0.45):
            lines[-1].append(w)
        else:
            lines.append([w])
    return lines


def _raw_field_values(rec: dict) -> list[str]:
    out: list[str] = []
    for k in RAW_FIELDS:
        v = rec.get(k)
        if v is None:
            continue
        s = str(v)
        if s:
            out.append(s)
    return out


def _inserted_space(word: str, field: str) -> bool:
    """원문 낱말이 raw 필드 안에서 공백으로 쪼개져 있는가."""
    if not word or not field:
        return False
    if word in field:
        return False
    wn = _compact(word)
    if len(wn) < 2:
        return False
    fn = _compact(field)
    idx = fn.find(wn)
    if idx < 0:
        return False
    compact_i = 0
    start: int | None = None
    for pos, ch in enumerate(field):
        if ch.isspace():
            continue
        if compact_i == idx and start is None:
            start = pos
        if start is not None and compact_i == idx + len(wn) - 1:
            return any(c.isspace() for c in field[start : pos + 1])
        compact_i += 1
    return False


def _note_events(
    words: list[tuple[float, float, float, float, str]],
    x0: float,
    x1: float,
) -> list[tuple[float, str]]:
    ev: list[tuple[float, str]] = []
    for y in _group_ys(words, x0, x1):
        ev.append((y, "group"))
    for y in _danga_ys(words, x0, x1):
        ev.append((y, "danga"))
    ev.sort(key=lambda e: e[0])
    return ev


def _in_note_at(y: float, events: list[tuple[float, str]], in_note: bool) -> bool:
    flag = in_note
    for ey, et in events:
        if ey >= y:
            break
        flag = et == "danga"
    return flag


def _end_in_note(events: list[tuple[float, str]], in_note: bool) -> bool:
    flag = in_note
    for _ey, et in events:
        flag = et == "danga"
    return flag


def _line_start_codes(
    words: list[tuple[float, float, float, float, str]],
    x0: float,
    x1: float,
) -> list[tuple[str, float, str]]:
    """같은 반·같은 줄에서 왼쪽에 낱말이 없는 공종코드. (code, yc, line_text)."""
    half = [w for w in words if x0 - 1 <= (w[0] + w[2]) / 2 < x1 + 1]
    out: list[tuple[str, float, str]] = []
    for line in _cluster_word_lines(half, gap=4.0):
        line = sorted(line, key=lambda w: w[0])
        if not line:
            continue
        first = line[0][4].strip()
        if not CODE_FULL.match(first):
            continue
        yc = sum((w[1] + w[3]) / 2.0 for w in line) / len(line)
        txt = " ".join(w[4] for w in line)
        out.append((first, yc, txt))
    return out


def _has_inherit_mark(s: str) -> bool:
    """extract.py 의 _is_inherit() 과 같은 기준(전부 상속 문자)으로 남은 상속 표시를 본다.

    글자 하나라도 포함되면(any) CHK'D· 4"처럼 규격에 흔한 작은따옴표·큰따옴표까지
    거짓양성으로 잡히므로, 필드 전체가 상속 문자로만 이뤄진 경우만 "안 풀린 상속"으로 본다.
    """
    t = re.sub(r"\s+", "", s or "")
    return bool(t) and all(c in INHERIT_CHARS for c in t)


def _has_any_inherit_char(s: str) -> bool:
    """name 전용: 상속 표시 문자가 한 글자라도 섞여 있으면 "안 풀린 상속"으로 본다.

    review_round1 반증(1)(2): 실명 앞뒤에 상속 문자 하나가 잔존해도(예: '＂1',
    '직접잔토처리/토사/ 굴착깊이 5m이하＂') _has_inherit_mark()(전부 일치)는 통과시킨다.
    name 필드는(spec 과 달리) 4권 실측(review_round1 재검)에서 CHK'D· 4" 같은 정당한
    따옴표 용례가 전혀 없으므로(spec 에만 22건 존재), name 에서는 any 기준을 써도
    거짓양성이 없다. spec 은 정당한 따옴표 용례가 있어 기존 all 기준을 유지한다.
    """
    return any(c in INHERIT_CHARS for c in (s or ""))


def _reparse_price(price_raw: str | None) -> int | None:
    """price_raw 를 독립적으로 재파싱(extract.py 의 함수를 다시 쓰지 않는다)."""
    if price_raw is None:
        return None
    t = str(price_raw).strip()
    if "폐지" in t:
        return None
    digits = t.replace(" ", "").replace(",", "")
    if digits.isdigit():
        return int(digits)
    return None


def _reparse_labor(labor_raw: str | None) -> float | None:
    if labor_raw is None:
        return None
    t = str(labor_raw).strip().replace(" ", "")
    m = LABOR_RE.match(t)
    return float(t[:-1]) if m else None


def _reparse_unit_norm(unit: str | None) -> str | None:
    if unit is None:
        return None
    return UNIT_NORM.get(unit, unit)


def _collapse_ws(s: str | None) -> str:
    """extract.py 의 _collapse() 와 같은 규칙(연속 공백 -> 한 칸, 양끝 자르기)을
    독립적으로 다시 구현한다(추출 코드의 함수를 그대로 불러 쓰지 않는다)."""
    return re.sub(r"\s+", " ", (s or "")).strip()


_HALF_RANK = {"L": 0, "C": 0, "R": 1}


def _order_key(pdf_page: Any, page_half: Any, y0: Any) -> tuple[int, int, float]:
    """레코드/소제목의 문서상 읽기 순서 키. 쪽(pdf_page) -> 단(L=0,C=0,R=1) -> 세로위치(y0)."""
    p = int(pdf_page) if pdf_page is not None else 0
    h = _HALF_RANK.get(page_half or "C", 0)
    y = float(y0) if y0 is not None else 0.0
    return (p, h, y)


def _inherit_source_gate(records: list[dict], subheaders: list[dict]) -> list[dict[str, Any]]:
    """review_round1 반증(5)·review_round2 실험 B(gate_false_pass) 에 대한 보강.

    RAW_FIELDS 에 최종 name 이 없어 낱말-보존 체크는 애초에 name 을 보지 않고,
    unresolved_inherit(전부 상속 문자) 은 임의의 오상속(엉뚱한 선조 이름을 이어받는
    경우)을 못 잡는다. review_round2 판정(같은 group_id 안에 '존재하는 값'인지만
    보고 '가장 최근인지'는 안 봄)은 여러 소제목이 섞인 그룹(잔토처리 표의 토사/
    리핑암/발파암(연암)/발파암(경암) 하위표 등)에서 앞선(틀린) 소제목으로 오상속돼도
    통과시켰다(실험 B로 재현).

    extract.py 의 상속 로직은 current_sub(가장 최근 소제목 ＊)가 한 번이라도 세워지면
    그 뒤 상속 레코드는 (그 사이 명시적 name 행이 몇 개 나왔든) 계속 그 소제목을
    이어받고, current_sub 가 전혀 없을 때만 직전 명시적 name(prev_name)을 이어받는다.
    이 게이트는 그 우선순위를 그대로 재현해 '이 레코드보다 앞선 것 중 가장 최근인'
    후보 단 하나만 정답으로 인정한다(순서 키는 subheaders.jsonl 의 page_half·y0,
    records.jsonl 의 page_half·bbox[1]로 추출 코드와 별도로 재구성).
    """
    from collections import defaultdict

    subs_by_group: dict[Any, list[tuple[tuple[int, int, float], str]]] = defaultdict(list)
    for sub in subheaders:
        key = _order_key(sub.get("pdf_page"), sub.get("page_half"), sub.get("y0"))
        subs_by_group[sub.get("group_id")].append((key, (sub.get("text") or "").strip()))
    for gid in subs_by_group:
        subs_by_group[gid].sort(key=lambda t: t[0])

    names_by_group: dict[Any, list[tuple[tuple[int, int, float], str]]] = defaultdict(list)
    for rec in records:
        if rec.get("name_inherited"):
            continue
        bbox = rec.get("bbox") or [0, 0, 0, 0]
        y0 = bbox[1] if len(bbox) == 4 else None
        key = _order_key(rec.get("pdf_page"), rec.get("page_half"), y0)
        names_by_group[rec.get("group_id")].append((key, (rec.get("name") or "").strip()))
    for gid in names_by_group:
        names_by_group[gid].sort(key=lambda t: t[0])

    out: list[dict[str, Any]] = []
    for rec in records:
        if not rec.get("name_inherited"):
            continue
        gid = rec.get("group_id")
        bbox = rec.get("bbox") or [0, 0, 0, 0]
        y0 = bbox[1] if len(bbox) == 4 else None
        key = _order_key(rec.get("pdf_page"), rec.get("page_half"), y0)
        name = (rec.get("name") or "").strip()
        item = {
            "code": rec.get("code"),
            "half": rec.get("half"),
            "pdf_page": rec.get("pdf_page"),
            "group_id": gid,
            "name": rec.get("name"),
        }
        subs_before = [t for k, t in subs_by_group.get(gid, []) if k < key]
        if subs_before:
            expected = subs_before[-1]
            if name != expected:
                out.append(dict(item, candidates=[expected], source="subheader"))
            continue
        names_before = [t for k, t in names_by_group.get(gid, []) if k < key]
        if names_before:
            expected = names_before[-1]
            if name != expected:
                out.append(dict(item, candidates=[expected], source="prev_name"))
        else:
            out.append(dict(item, candidates=[], source="none"))
    return out


def _field_gates(
    records: list[dict], subheaders: list[dict] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """H2: 최종 필드 불변식. 원문 낱말(raw)이 아니라 name·spec·price·labor_ratio·unit_norm
    최종 값 자체가 맞는지를 본다(추출 코드의 파싱 함수를 재사용하지 않고 독립적으로 다시 판정).

    review_round2 실험 A(gate_false_pass): 비상속(name_inherited=false) 리프 레코드는
    price/labor_ratio/unit_norm 만 재파싱하던 기존 parse_mismatch 로는 name 이 통째로
    엉뚱한 문자열로 바뀌어도(빈칸도 상속기호도 아니면) 전혀 안 잡혔다. extract.py 는
    비상속 행의 name 을 name = _collapse(name_raw) 로만 만드므로, 그 관계를
    name_raw -> name 재계산으로 독립 검증한다(상속 행의 name 은 name_raw 에서 오지
    않으므로 대상이 아니다 — 그쪽은 _inherit_source_gate 가 맡는다). spec 은 상속이
    없어 모든 레코드에 같은 방식으로 적용한다.
    """
    empty_name: list[dict[str, Any]] = []
    unresolved_inherit: list[dict[str, Any]] = []
    parse_mismatch: list[dict[str, Any]] = []
    for rec in records:
        status = (rec.get("status") or "").strip()
        item = {"code": rec.get("code"), "half": rec.get("half"), "pdf_page": rec.get("pdf_page")}
        if status in ("present", "abolished") and not (rec.get("name") or "").strip():
            empty_name.append(dict(item, name_raw=rec.get("name_raw")))
        if _has_any_inherit_char(rec.get("name") or "") or _has_inherit_mark(rec.get("spec") or ""):
            unresolved_inherit.append(dict(item, name=rec.get("name"), spec=rec.get("spec")))
        mism = {}
        exp_price = _reparse_price(rec.get("price_raw"))
        if exp_price != rec.get("price"):
            mism["price"] = {"expected": exp_price, "actual": rec.get("price"), "raw": rec.get("price_raw")}
        exp_labor = _reparse_labor(rec.get("labor_raw"))
        if exp_labor != rec.get("labor_ratio"):
            mism["labor_ratio"] = {
                "expected": exp_labor,
                "actual": rec.get("labor_ratio"),
                "raw": rec.get("labor_raw"),
            }
        exp_unit = _reparse_unit_norm(rec.get("unit"))
        if exp_unit != rec.get("unit_norm"):
            mism["unit_norm"] = {"expected": exp_unit, "actual": rec.get("unit_norm"), "unit": rec.get("unit")}
        if not rec.get("name_inherited"):
            exp_name = _collapse_ws(rec.get("name_raw"))
            if exp_name != (rec.get("name") or ""):
                mism["name"] = {"expected": exp_name, "actual": rec.get("name"), "raw": rec.get("name_raw")}
        spec_raw = rec.get("spec_raw")
        if spec_raw is not None:
            spec_raw_s = str(spec_raw)
            exp_spec = _collapse_ws(spec_raw_s) if spec_raw_s.strip() else spec_raw_s.strip()
            if exp_spec != (rec.get("spec") or ""):
                mism["spec"] = {"expected": exp_spec, "actual": rec.get("spec"), "raw": rec.get("spec_raw")}
        if mism:
            parse_mismatch.append(dict(item, mismatch=mism))
    return {
        "empty_name": empty_name,
        "unresolved_inherit": unresolved_inherit,
        "parse_mismatch": parse_mismatch,
        "inherit_mismatch": _inherit_source_gate(records, subheaders or []),
    }


# N8: 【단가정의】 주석 낱말 보존 게이트. extract.py 의 함수를 재사용하지 않고
# 이 파일 자체의 자료(words/drawings/images)만으로 독립적으로 다시 판정한다.
_NOTE_ARROW_RE = re.compile(r"^[가-힣]{1,6}방향→?$")
_NOTE_DIST_RE = re.compile(r"^\d+(?:\.\d+)?m$")
# review_round3 wrong_notes(2025H1 p106 JM잡철물): extract.py DIAGRAM_LABEL_RE 와
# 같은 눈높이로, 대괄호 안(또는 조각)에 영문·숫자가 있으면("<XG-21"·"규격>") 예시도
# 비교 라벨이 아니라 규격 목록 표제이므로 도식 낱말로 보지 않는다.
_NOTE_BRACKET_RE = re.compile(r"^<[^<>0-9A-Za-z]{0,12}>?$|^[^<>0-9A-Za-z]{0,12}>$")
_NOTE_QUALITY_RE = re.compile(r"^(?:양호|보통|불량)$")
_NOTE_EVAL_RE = re.compile(r"^(?:부적정|적\s*정)$")
# G02i2 F3: extract.py DIAGRAM_EVAL_REASON_RE 와 같은 기준 — 평가어 뒤에 사유
# 괄호가 붙은 예시도 캡션 줄 전체("부적정 (사유: 구간에 따라 암질별로 분류하여
# 구간별 적용)")를 한 줄 신호로 잡는다(단어 하나짜리 _NOTE_EVAL_RE 는 "부적정"만
# 잡고 "(사유:"·"구간에" 같은 나머지 낱말은 놓친다).
_NOTE_EVAL_REASON_RE = re.compile(r"^(?:부적정|적정)\(사유[:：]")


class _CachedTable:
    """CLI/extract 가 넘긴 find_tables() 스냅샷. 게이트가 extract 판정 함수를
    부르지 않고 bbox·col_count·extract()·rows.cells 만 재사용한다."""

    def __init__(self, snap: dict[str, Any]):
        self.bbox = snap["bbox"]
        self.col_count = snap["col_count"]
        self._extract_rows = snap.get("extract") or []
        self.rows = [type("_Row", (), {"cells": row})() for row in (snap.get("row_cells") or [])]

    def extract(self) -> list:
        return self._extract_rows


def snapshot_page_tables(tables: list) -> list[dict[str, Any]]:
    """find_tables() Table 목록을 문서가 닫혀도 쓸 수 있는 스냅샷으로."""
    snaps: list[dict[str, Any]] = []
    for t in tables:
        try:
            extracted = t.extract() or []
        except Exception:  # noqa: BLE001
            extracted = []
        try:
            row_cells = [[tuple(float(v) for v in c) for c in r.cells] for r in t.rows]
        except Exception:  # noqa: BLE001
            row_cells = []
        snaps.append(
            {
                "bbox": tuple(float(v) for v in t.bbox),
                "col_count": int(getattr(t, "col_count", 0) or 0),
                "extract": extracted,
                "row_cells": row_cells,
            }
        )
    return snaps


def _coerce_tables(items: list | None) -> list:
    if not items:
        return []
    out = []
    for t in items:
        out.append(_CachedTable(t) if isinstance(t, dict) else t)
    return out


def _image_boxes(page: fitz.Page, errors: list | None = None) -> list[tuple[float, float, float, float]]:
    try:
        return [tuple(float(v) for v in im["bbox"]) for im in page.get_image_info()]
    except Exception as e:  # noqa: BLE001
        pno = int(getattr(page, "number", -1)) + 1
        _LOG.warning("get_image_info failed (page %s): %s", pno, e)
        if errors is not None:
            errors.append({"page": pno, "where": "get_image_info", "error": str(e)})
        return []


def _page_tables(page: fitz.Page, cached: list | None = None, errors: list | None = None) -> list:
    """이 쪽의 find_tables() 결과(Table 목록). 캐시가 있으면 재사용하고
    없을 때만 쪽마다 다시 부른다."""
    if cached is not None:
        return _coerce_tables(cached)
    try:
        tabs = page.find_tables()
    except Exception as e:  # noqa: BLE001
        pno = int(getattr(page, "number", -1)) + 1
        _LOG.warning("find_tables failed (page %s): %s", pno, e)
        if errors is not None:
            errors.append({"page": pno, "where": "find_tables", "error": str(e)})
        return []
    return list(tabs.tables) if tabs else []


def _diagram_legend_boxes(
    page: fitz.Page, tables: list | None = None
) -> list[tuple[float, float, float, float]]:
    """extract.py 의 판정을 재사용하지 않고 이 파일 스스로 find_tables() 를 불러,
    헤더 행이 품질등급(양호/보통/불량)만으로 이뤄진 표(수량산출 예시도의 범례)
    상자를 독립적으로 다시 찾는다."""
    boxes: list[tuple[float, float, float, float]] = []
    tabs_tables = _page_tables(page) if tables is None else tables
    labels = {"양호", "보통", "불량"}
    for t in tabs_tables:
        try:
            rows = t.extract() or []
        except Exception:  # noqa: BLE001
            continue
        ext0 = rows[0] if rows else []
        row0 = [re.sub(r"\s+", "", str(c or "")) for c in ext0]
        full_txt = " ".join(str(c or "") for row in rows for c in row)
        # extract.py 와 같은 기준(review_round1 wrong_notes: 2025H2 p74·76·85·86):
        # 사례1(품질등급 5열, "보통" 포함) 뿐 아니라, 코드+거리구간(예: 550-750m,
        # L=70m～100m이하) 표시로만 이뤄지고 콤마 붙은 단가가 전혀 없는 표(사례2
        # 4열 예시도 등)도 실제 가격표가 아니라 예시도가 find_tables() 에 표로
        # 잘못 잡힌 것이므로 그림 상자로 본다. 1행짜리 제한을 없앤다(N2 확장).
        is_legend = (
            t.col_count >= 4
            and row0
            and all(x in labels or x == "" for x in row0)
            and any(x in labels for x in row0)
        )
        has_price_comma = bool(re.search(r"\d{1,3}(?:,\d{3})+", full_txt))
        has_dist_range = bool(
            re.search(r"\d+(?:\.\d+)?m\s*[~∼\-]\s*\d+(?:\.\d+)?m", full_txt)
            or re.search(r"L\s*=\s*\d+(?:\.\d+)?m", full_txt)
            or re.search(r"\d+(?:\.\d+)?\s*[~∼\-]\s*\d+(?:\.\d+)?m(?!\S)", full_txt)
        )
        is_dist_legend = bool(CODE_RE.search(full_txt)) and has_dist_range and not has_price_comma
        if is_legend or is_dist_legend:
            boxes.append(tuple(float(v) for v in t.bbox))
    return boxes


def _note_subtables(
    page: fitz.Page,
    tables: list | None = None,
    legend_boxes: list[tuple[float, float, float, float]] | None = None,
) -> list[list[list[tuple[float, float, float, float]]]]:
    """G02i2 2차(코치 재검, 2024H1 p45#107 "ED*** 거푸집" ② order_mismatch):
    주석 안 작은 표(가격표·범례·대분류 배너 제외)의 행별 칸 bbox
    ([행][칸] = (x0,y0,x1,y1)). extract.py 의 _flatten_subtable() 이 읽는
    표와 같은 표를 이 파일 스스로 find_tables() 로 다시 찾는다(재사용 아님).

    legend_boxes(_diagram_legend_boxes 결과, 예: 2024H1 p35 "시공기준면"
    예시도의 코드+거리 범례)는 반드시 함께 넘겨 제외한다 — 안 넘기면 이
    함수가 "공종코드"·"대분류" 만 걸러 예시도까지 진짜 표로 오인해서,
    호출부가 그 도식 낱말들을 표 칸 취급해 그림 줄 판정을 건너뛰게
    만든다(2024H1 p35~36 재현: notes_missing 다수 발생).
    """
    tabs_tables = _page_tables(page) if tables is None else tables
    lbx = legend_boxes or []
    out: list[list[list[tuple[float, float, float, float]]]] = []
    for t in tabs_tables:
        tb = tuple(float(v) for v in t.bbox)
        if any(_bbox_overlap(tb, lb, tol=1.0) for lb in lbx):
            continue
        try:
            rows_extract = t.extract() or []
        except Exception:
            rows_extract = []
        ext0 = rows_extract[0] if rows_extract else []
        head = " ".join(str(c or "") for c in ext0)
        if "공종코드" in head or "공종명칭" in head or "공종명" in head:
            continue
        full_txt = " ".join(str(c or "") for row in rows_extract for c in row)
        if "대분류" in full_txt.replace(" ", ""):
            continue
        if t.col_count >= 12:
            continue
        # G02i2 2차(코치 재검, 2024H1 p35~36 시공기준면 예시도 재현):
        # legend_boxes 는 "양호/보통/불량" 등급표·"m~m" 범위 표시가 있는
        # 표만 그림으로 본다 — 코드(DE149.10000 등)와 낱개 거리표시(4m·6m·
        # 10m, 범위 기호 없음)만 늘어선 예시도 격자는 그 기준을 통과 못 해
        # legend_boxes 에 안 잡히면서도 find_tables() 엔 표로 잡혔다. 진짜
        # 주석 작은 표(이 문서에서 실제로 본 사례들)는 항상 문장·서술어가
        # 있어 한글 글자 수가 많다 — 한글이 거의 없는(전부 코드·숫자·기호인)
        # 표는 표가 아니라 도식으로 보고 여기서도 제외한다.
        if len(re.findall(r"[가-힣]", full_txt)) < 8:
            continue
        # G02i2 2차(코치 재검, 2025H1 p73 Ø10m 미만/이상 예시도 재현): 위 한글
        # 글자수 기준만으로는 "(깊이 할증 적용)" 같은 짧은 괄호 설명이 두 번
        # 있는 예시도(한글 6자×2=12자)까지 통과했다. 이 문서에서 실제 주석
        # 작은 표는 칸 값에 공종코드(NA209.11320 등)가 섞인 적이 없다 — 예시도
        # 격자만 코드를 쓴다(칸 하나에 코드 여러 줄+괄호 설명이 함께 있는 경우도
        # 있어 칸 전체 일치가 아니라 어디에든 있는지로 본다). 코드가 하나라도
        # 있으면 표로 보지 않는다.
        if CODE_RE.search(full_txt):
            continue
        try:
            row_cells = [[tuple(float(v) for v in c) for c in r.cells] for r in t.rows]
        except Exception:
            continue
        if row_cells:
            out.append(row_cells)
    return out


def _align_subtable_row_words(
    words: list[tuple[float, float, float, float, str]],
    subtables: list[list[list[tuple[float, float, float, float]]]],
) -> list[tuple[float, float, float, float, str]]:
    """G02i2 2차(코치 재검, 2024H1 p45#107): 표 한 행 안에서 칸마다 내용
    줄 수가 다르면(예: "구분" 칸은 한 줄 "매끈한마감", "적용기준" 칸은 세 줄
    "T형보,…\\n파라펫트,…\\n(견고하고…)") PDF 는 짧은 칸을 그 행 높이
    한가운데로 세로 정렬해 찍는다 — 실제 글자 y 가 키 큰 칸의 첫 줄보다
    한참 아래에 있어, y 순서로만 읽으면(_cluster_word_lines) 짧은 칸이 그
    행의 다른 칸들과 한 줄로 안 묶이고 늦게(뒤섞여) 읽힌다. extract.py 가
    표를 뽑을 때 쓰는 것과 같은 행 bbox(find_tables())를 이 파일도 다시
    구해, 그 행의 실제 위쪽 시작(행 bbox 의 y0, 대개 가장 큰 칸의 첫 줄과
    거의 같다)보다 한참(>8pt) 아래에서 시작하는 칸만 그 차이만큼 위로
    당겨 정렬용 y 를 만든다 — x·글자·칸 안 여러 줄의 상대 순서는 그대로
    둔다(한 줄짜리 짧은 칸만 흔히 해당하므로 실질적으로 안전하다)."""
    if not subtables:
        return words
    shifts: dict[int, float] = {}
    for row_cells in subtables:
        for cells in row_cells:
            if not cells:
                continue
            row_y0 = min(c[1] for c in cells)
            for cx0, cy0, cx1, cy1 in cells:
                idxs = [
                    i
                    for i, w in enumerate(words)
                    if cx0 - 1 <= (w[0] + w[2]) / 2 <= cx1 + 1
                    and cy0 - 1 <= (w[1] + w[3]) / 2 <= cy1 + 1
                ]
                if not idxs:
                    continue
                cell_top = min(words[i][1] for i in idxs)
                offset = cell_top - row_y0
                if offset > 8.0:
                    for i in idxs:
                        shifts[i] = offset
    if not shifts:
        return words
    out = list(words)
    for i, offset in shifts.items():
        wx0, wy0, wx1, wy1, wt = out[i]
        out[i] = (wx0, wy0 - offset, wx1, wy1 - offset, wt)
    return out


def _is_figure_word(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    text: str,
    image_boxes: list[tuple],
    alone_on_line: bool = False,
) -> bool:
    """이 낱말이 그림·도식 영역(이미지 겹침·화살표·거리표시·비교대괄호·품질등급
    단독 토큰) 안에 있어 주석 항목·표·그림 제목 어디에도 들어가지 않는가."""
    t = text.strip()
    if not t:
        return False
    xc, yc = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    # G02i2 F5 gate_false_pass 보강(코치 독립 시험 T05): 이미지 bbox 겹침만으로
    # "그림 속 낱말"로 보던 규칙을 없앤다. review 표본 두 개가 서로 다른
    # 방향으로 반증했다 — (1) 2024H1#p25#37: 캡션 없는 사진이 30pt 여유 밖의
    # 진짜 주석 문장까지 삼켰다(margin 을 좁혀도 완전히는 못 없앰). (2)
    # 2024H1#p48#117/118, p49#120/121: 스캔·삽입된 사진의 PDF bbox 자체가
    # 실제 그림보다 넉넉해(여백 포함) "[표준도] 우수맨홀(900×900) …" 같은
    # 진짜 항목 문장(groups.jsonl 에도 그대로 있고 extract.py 도 이 글을
    # 배너·그림 제목으로 빼지 않는다)이 그 bbox "안"에 물리적으로 들어가
    # 버렸다. 래스터 이미지 안 픽셀 글자는애초에 page.get_text("words") 에
    # 잡히지 않으므로(글자 개체가 아니라 그림이라) 이 검사가 막던 진짜
    # 사각지대(이미지에 얹힌 실제 텍스트 개체)는 없고, 있는 건 이 오탐뿐이다.
    # 진짜 도식 낱말은 아래 화살표·거리표시·비교대괄호·품질등급 규칙과
    # _line_has_diagram_signal() 의 줄 단위 규칙이 여전히 잡는다.
    if t in ("<", ">"):
        return True
    # G02i2 F5 gate_false_pass 보강(코치 독립 시험 T05, review 표본
    # 2024H1#p32#65 "100m 소운반", p36#83 "1.5m 이하", p66#182 "5m 이하"):
    # "Nm" 단독 낱말 규칙(_NOTE_DIST_RE)을 문맥과 무관하게 낱말 하나만 보고
    # 어디서나 적용하면, 예시도와 무관하게 진짜 주석 문장 속에 있는 정상적인
    # 거리 표기("100m 소운반을 포함한다")까지 그림 낱말로 잘못 빼버린다.
    # 이 문서의 예시도(F3, 이미지 없이 벡터로 그려진 굴진방향 눈금)는 거리
    # 눈금이 항상 같은 줄에 여러 개 늘어서 있어("100m 200m 300m…")
    # _line_has_diagram_signal() 의 줄 단위 규칙(len>=2 전부 DIST/QUALITY)
    # 만으로 이미 다 잡힌다 — "Nm" 단독 낱말 규칙만 없앤다. "부적정"·"양호"
    # 같은 품질등급/평가어 단독 규칙은 그대로 둔다(review 재검: 2024H1
    # p56#155·p63#175 의 예시도 "부적정" 라벨이 사유 문구 없이 단독으로도
    # 나와, 줄 단위 규칙만으로는 못 잡고 이 낱말 단위 규칙이 꼭 필요했다 —
    # 되돌려 notes_missing 2건을 0으로 확인).
    # G02i2 F5 gate_false_pass 추가 보강(코치 독립 시험 T05, review 표본
    # 2024H1#p56#155 ④ "…일축압축강도 1,200㎏/㎠~2,400㎏/㎠ : 양호 - …"): 같은
    # 그룹, 심지어 같은 쪽 안에서도 "양호"·"보통"·"불량"·"적정"·"부적정" 이
    # 예시도의 단독 라벨로도, 항목 문장 속 값("：양호")으로도 둘 다 나온다
    # (예시도가 "다음 쪽"이 아니라 항목 바로 아래 같은 쪽에 있는 경우도
    # 있어, 세로 거리만으로는 못 가른다 — 실측 반증). 실제로 다른 점은:
    # 예시도의 라벨은 그 줄에 이 낱말 하나뿐이고(같은 반·같은 y 줄에 다른
    # 낱말이 전혀 없음), 항목 문장 속 값은 항상 앞뒤에 다른 낱말이 있는
    # 문장의 한가운데다. 그 줄에 이 낱말 혼자일 때만 그림으로 본다.
    if alone_on_line and _NOTE_QUALITY_RE.fullmatch(t):
        return True
    if alone_on_line and _NOTE_EVAL_RE.fullmatch(t):
        return True
    if _NOTE_ARROW_RE.fullmatch(t) or t == "시공기준면":
        return True
    # G02i2 F5 gate_false_pass 보강(코치 독립 시험 T05, review 표본
    # 2024H1#p98#276 "<XG-21 규격>"): _NOTE_BRACKET_RE 의 두 번째 갈래
    # (앞에 "<" 없이 한글만 있다가 ">"로 끝나는 조각)는 "규격>"처럼
    # "<XG-21"(영문·숫자, 앞선 낱말)과 짝지어 진짜 규격 라벨을 이루는
    # 낱말까지 잡았다 — 같은 줄에 다른 낱말(영문 규격 코드)이 있으면
    # 진짜 항목 문장이므로, 이 낱말이 줄에 혼자일 때만 그림 조각으로 본다
    # ("<사례1>"·"<부적정>"류 진짜 도식 라벨은 늘 혼자다).
    if alone_on_line and _NOTE_BRACKET_RE.fullmatch(t) and len(t) <= 14:
        return True
    return False


def _note_bands(
    page: fitz.Page, bodies: list[tuple[float, float, float, float]]
) -> list[tuple[str, float, float, float, float]]:
    """(page_half, x0, hx1, y0, y1) 표 본문이 아닌, 주석이 있을 수 있는 구간."""
    out: list[tuple[str, float, float, float, float]] = []
    bottom_limit = page.rect.height * 0.92
    for ph, hx0, hx1 in _halves(page):
        half_bodies = sorted(
            (b for b in bodies if b[0] < hx1 - 1 and b[2] > hx0 + 1),
            key=lambda b: b[1],
        )
        cursor = 6.0
        for b in half_bodies:
            if b[1] - cursor > 3.0:
                out.append((ph, hx0, hx1, cursor, b[1]))
            cursor = max(cursor, b[3])
        if bottom_limit - cursor > 3.0:
            out.append((ph, hx0, hx1, cursor, bottom_limit))
    return out


def _note_line_is_structural(text: str) -> bool:
    """■ 그룹 머리글·대분류 배너·【단가정의】 라벨 줄(주석 내용이 아니라 구조
    표지)인가. 이런 줄은 애초에 notes 항목에도 들어가지 않으므로 낱말 보존
    검사에서도 뺀다."""
    t = text.strip()
    if not t:
        return True
    compact = t.replace(" ", "")
    if t.startswith("■") or compact.startswith("■"):
        return True
    if compact.startswith("대분류"):
        return True
    if re.fullmatch(r"-\s*\d+\s*-", t):
        return True
    if "【단가정의】" in compact or compact == "단가정의":
        return True
    # extract.py _is_structural_note_junk 와 같은 눈높이(review_round1
    # N8-scope-suspect): 표 소구간 표제("- 초기굴진"·"- 굴진 총연장 150m이하")와
    # 분야 전환 표지도 애초에 notes 항목에 안 들어가므로 게이트에서도 뺀다.
    if t.startswith("- ") and re.search(r"(총연장|초과|이하)", t) and "단가" not in t:
        # K3: extract.py 와 글자 그대로 같은 넓은 규칙을 쓰지 않는다.
        # 짧은 제목 꼴(문장 종결 아니고 길이 상한)만 구조 표지로 본다 — 진짜
        # 문장을 버린 추출기 결함을 잡기 위함.
        if len(t) <= 48 and not re.search(r"(다|함|음|임|됨|이다|한다|된다)\.\s*$", t):
            return True
    if re.fullmatch(r"-\s*(초기굴진|본굴진|도달굴진)", t):
        return True
    if "자체표준시장단가" in compact:
        return True
    if re.fullmatch(r"[가-힣·ㆍ‧･․]{2,12}분야", compact):
        return True
    if re.fullmatch(r"\d{4}\.\s*\d{1,2}", t):
        return True
    # extract.py 와 같은 기준: 대괄호 표준도 라벨만 있고 그 안에 실제 문장이
    # 없는 줄("[표준도]" 등)은 애초에 notes·figures 어디에도 안 들어간다
    # (_is_structural_note_junk 의 같은 조건 참고).
    if re.fullmatch(r"\[[^\[\]]{1,20}\]", t):
        return True
    if t.startswith("○") or compact.startswith("○"):
        return True
    if compact.startswith("목차") or t.startswith("목차"):
        return True
    first_tok = t.split()[0] if t.split() else ""
    if CODE_RE.match(first_tok) or re.match(r"^[A-Z]{2}\d+\*", first_tok):
        return True
    # 색인 줄 머리 장식(n DH419.11505 …) — 항목 기호 ① 이 아님
    if re.match(r"^n\s*[A-Z]{2}\d", t.strip()):
        return True
    return False


def _band_source_ends_owner(
    band_words: list[tuple[float, float, float, float, str]],
    by0: float,
    bodies: list[tuple[float, float, float, float]],
    hx0: float,
    hx1: float,
) -> bool:
    """신호 없는 밴드를 건너뛸지 — 산출이 아니라 원문 구조 신호로만 판정.

    이 밴드가 (a) 다음 ■ 머리글·대분류 배너·표 시작·분야 표지·쪽 번호 머리말
    뒤에 있거나 (b) 그룹 종결을 뜻하는 구조 요소 뒤에 있으면 True(owner 없음).
    그 밖의 신호 없는 밴드는 직전 owner 의 이어짐이므로 False.
    """
    if not band_words:
        return True
    for b in bodies:
        if b[2] <= hx0 + 1 or b[0] >= hx1 - 1:
            continue
        if b[3] <= by0 + 2.0 and (by0 - b[3]) < 30.0:
            return True
    lines = _cluster_word_lines(band_words)
    for line in lines:
        sorted_line = sorted(line, key=lambda w: w[0])
        txt = " ".join(w[4] for w in sorted_line).strip()
        if not txt:
            continue
        compact = txt.replace(" ", "")
        # 쪽 번호 한 줄은 머리말로 건너뛰고, 그것만으로 밴드 전체를 버리지는 않는다
        # (이어짐 쪽 맨 위 `- N -` 뒤에 진짜 문장이 오는 경우가 있다).
        if re.fullmatch(r"-\s*\d+\s*-", txt):
            continue
        if txt.startswith("■") or compact.startswith("■"):
            return True
        if compact.startswith("대분류"):
            return True
        core = compact.lstrip("○●•∙·")
        if core.startswith("대분류"):
            return True
        if txt.startswith("○") or compact.startswith("○"):
            return True
        if txt.startswith("목차") or compact.startswith("목차"):
            return True
        if "자체표준시장단가" in compact:
            return True
        if re.fullmatch(r"[가-힣·ㆍ‧･․]{2,12}분야", compact):
            return True
        if re.fullmatch(r"\d{4}\.\s*\d{1,2}", txt):
            return True
        if "공종코드" in compact or "공종명칭" in compact or "공종명" in compact:
            return True
        first_tok = txt.split()[0] if txt.split() else ""
        if CODE_RE.match(first_tok) or re.match(r"^[A-Z]{2}\d+\*", first_tok):
            return True
        if any(CODE_RE.match(tok) or re.match(r"^[A-Z]{2}\d+\*", tok) for tok in txt.split()[:3]):
            return True
        return False
    return True


def _line_has_diagram_signal(
    line: list[tuple[float, float, float, float, str]], image_boxes: list[tuple]
) -> bool:
    """그림·도식 줄 신호가 한 줄(같은 y 로 묶인 낱말들) 안 어디에든 있으면
    그 줄 전체를 그림 영역으로 본다(N2 는 줄 단위로 도식 라벨을 뺀다)."""
    texts = [w[4].strip() for w in line if w[4].strip()]
    if not texts:
        return False
    joined = " ".join(texts)
    joined_compact = joined.replace(" ", "")
    # G02i2 F5 gate_false_pass 보강(코치 독립 시험 T05): "<"·">"가 줄
    # 어디에나 있기만 하면 그림으로 보던 규칙은 "<사례1>"·"<사례2>"(순수
    # 한글+숫자)나 "<부적정>"·"<적정>"(순수 한글, review 표본 2024H1
    # p35#78~p36#81 의 암질평가 범례 "< 부적정 > < 적정 >") 같은 진짜
    # 도식·범례 라벨을 잡으려던 것인데, "<XG-21 규격>"처럼 영문(부품
    # 규격 코드)이 꺾쇠 안에 든 진짜 항목 문장(review 표본 2024H1 p98#276,
    # groups.jsonl 에도 그대로 있고 extract.py 도 이 글을 빼지 않는다)까지
    # 통째로 삼켰다. review 재검(2025H1 p73 "<Ø10m 미만>"·"<Ø10m 이상>"):
    # "m"(미터 단위, 소문자)까지 영문으로 걸러내면 도식 라벨도 다시 못
    # 잡는다 — 이 문서에서 대문자 영문(XG 처럼)이 있으면 늘 규격 코드고,
    # 도식·범례의 꺾쇠 라벨은 소문자 단위(m)뿐이거나 아예 영문이 없다.
    # 대문자 영문(A-Z)이 하나도 없을 때만 이 규칙을 적용한다.
    if "<" in joined and ">" in joined and not re.search(r"[A-Z]", joined):
        return True
    if _NOTE_EVAL_REASON_RE.match(joined_compact):
        return True
    # G02i2 F5 gate_false_pass 보강(코치 독립 시험 T05, review 표본
    # 2024H1#p56#155·p63#175 예시도의 사유 없는 "적정" 라벨): 자간 탓에
    # "적"·"정"이 완전히 다른 두 낱말로 갈라지면(같은 줄에 그 둘뿐,
    # "(사유:…" 도 없음) 어느 한쪽 낱말만으로는 _NOTE_EVAL_RE(전체
    # "부적정"·"적정" 일치)가 못 잡는다 — 줄 전체 낱말을 공백 없이 이어
    # "부적정"·"적정"과 정확히 같을 때(그 줄에 정말 이것뿐일 때만)도
    # 그림 라벨로 본다.
    if _NOTE_EVAL_RE.fullmatch(joined_compact) or _NOTE_QUALITY_RE.fullmatch(joined_compact):
        return True
    if any(_NOTE_ARROW_RE.fullmatch(t) or t == "시공기준면" for t in texts):
        return True
    if len(texts) >= 2 and all(
        _NOTE_QUALITY_RE.fullmatch(t) or _NOTE_DIST_RE.fullmatch(t) for t in texts
    ):
        return True
    # 예시도 안에서 코드 낱말 여럿이 (가격·설명 없이) 같은 y 에 늘어선 경우
    # (2025H2 p50 "시공기준면" 예시도의 DE149.10000·DE139.10000 등, 서로 다른
    # x 라 같은 "줄"로 뭉쳐도 len(line)==1 조건에는 안 걸렸다).
    if texts and all(CODE_FULL.fullmatch(t) for t in texts):
        return True
    # 코드·거리·품질등급 낱말이 한 줄에 섞여 늘어선 경우(2025H1 p73
    # "5m NA209.11320" 처럼 예시도의 거리 눈금과 코드가 같은 y 에 있음).
    if len(texts) >= 2 and all(
        CODE_FULL.fullmatch(t) or _NOTE_DIST_RE.fullmatch(t) or _NOTE_QUALITY_RE.fullmatch(t)
        for t in texts
    ):
        return True
    # 예시도의 짧은 괄호 설명 한 줄("(깊이 할증 적용)" 등 20자 이하). 예시도
    # 두 개가 같은 y 에 나란히 있으면(2025H1 p73 Ø10m 미만/이상 예) 이 줄
    # 하나에 같은 괄호가 반복돼 들어온다.
    if re.fullmatch(r"(?:\([^()]{1,18}\))+", joined.replace(" ", "")):
        return True
    if len(line) == 1:
        wx0, wy0, wx1, wy1, t = line[0]
        if CODE_FULL.fullmatch(t.strip()):
            xc, yc = (wx0 + wx1) / 2.0, (wy0 + wy1) / 2.0
            for ix0, iy0, ix1, iy1 in image_boxes:
                if ix0 - 60 <= xc <= ix1 + 60 and iy0 - 80 <= yc <= iy1 + 80:
                    return True
            # 한 줄에 코드 하나뿐이고(같은 줄에 가격·설명이 전혀 없음) 이는
            # 실제 주석 문장이 아니라 예시도의 코드 라벨이다.
            return True
    return False


def _group_destinations(g: dict) -> tuple[str, str, str, str]:
    """그룹 하나의 주석 목적지: (주석 항목 글[머리글 포함], 주석 속 작은 표,
    그림 제목, 실제 주석 문장만[머리글 제외])."""
    header_parts = [str(g.get("header_raw") or ""), str(g.get("major_name") or "")]
    item_parts: list[str] = list(header_parts)
    notes_only_parts: list[str] = []
    table_parts: list[str] = []
    for n in g.get("notes") or []:
        if n.get("subtable"):
            table_parts.append(str(n.get("item") or ""))
            table_parts.append(str(n.get("item_raw") or ""))
        else:
            item_parts.append(str(n.get("item") or ""))
            item_parts.append(str(n.get("item_raw") or ""))
            notes_only_parts.append(str(n.get("item") or ""))
            notes_only_parts.append(str(n.get("item_raw") or ""))
    fig_blob = "\n".join(str(f.get("caption") or "") for f in (g.get("figures") or []))
    return "\n".join(item_parts), "\n".join(table_parts), fig_blob, "\n".join(notes_only_parts)


def _group_output_chars(g: dict, pages: tuple[int, int] | None = None) -> str:
    """G02i2 F5: 그룹 notes 를 출력(읽기) 순서 그대로 이어 붙인 공백 없는 문자열.

    부표 항목은 합성 구분자를 떼고 칸 글자만 남긴다.
    pages 가 있으면 그 범위 안 pdf_page 항목만 넣는다(K2 부분 실행).
    """
    parts: list[str] = []
    lo, hi = pages if pages is not None else (None, None)
    for n in g.get("notes") or []:
        if lo is not None and hi is not None:
            np = n.get("pdf_page")
            if not isinstance(np, int) or not (lo <= np <= hi):
                continue
        item = str(n.get("item") or "")
        if n.get("subtable") or item.startswith("(표)") or item.startswith("[표]"):
            body = item
            for pre in ("(표)", "[표]"):
                if body.startswith(pre):
                    body = body[len(pre) :]
                    break
            body = body.replace("|", " ").replace("/", " ")
            parts.append(body)
        else:
            parts.append(item)
    return _compact("".join(parts))


_PUA_RE = re.compile(r"[-]")


def _pua_scan(result: dict[str, Any]) -> list[dict[str, Any]]:
    """G02i2 F5: records·subheaders·groups 모든 문자열에 남은 사용자 정의
    영역(PUA, U+E000~U+F8FF) 글자를 센다(kepco_pua_scan.py 코치 도구와 같은 범위,
    독립 실행)."""
    hits: list[dict[str, Any]] = []

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, str):
            for m in _PUA_RE.finditer(obj):
                hits.append({"path": path, "char": f"U+{ord(m.group()):04X}"})
        elif isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, path + "[]")

    for key in ("records", "subheaders", "groups"):
        for row in result.get(key) or []:
            walk(row, key)
    return hits


def _group_reading_order(
    groups: list[dict], records: list[dict]
) -> dict[tuple[int, str], list[tuple[float, float, dict]]]:
    """(pdf_page, page_half) -> [(그룹의 이 페이지·단 안 최소 y0, 최대 y1, 그룹), ...] y순 정렬.

    같은 그룹이 여러 쪽·단에 표를 가질 수 있어 페이지·단마다 따로 묶는다.
    """
    recs_by_group: dict[Any, list[dict]] = {}
    for r in records:
        recs_by_group.setdefault(r.get("group_id"), []).append(r)

    by_key: dict[tuple[int, str], dict[str, list[float]]] = {}
    for g in groups:
        gid = g.get("group_id")
        for r in recs_by_group.get(gid, []):
            bbox = r.get("bbox") or [0, 0, 0, 0]
            if len(bbox) != 4:
                continue
            key = (int(r["pdf_page"]), r.get("page_half") or "C")
            slot = by_key.setdefault(key, {})
            rng = slot.get(gid)
            y0, y1 = float(bbox[1]), float(bbox[3])
            if rng is None:
                slot[gid] = [y0, y1]
            else:
                rng[0] = min(rng[0], y0)
                rng[1] = max(rng[1], y1)

    gmap = {g.get("group_id"): g for g in groups}
    out: dict[tuple[int, str], list[tuple[float, float, dict]]] = {}
    for key, slot in by_key.items():
        rows = [(y0, y1, gmap[gid]) for gid, (y0, y1) in slot.items() if gid in gmap]
        rows.sort(key=lambda t: t[0])
        out[key] = rows
    return out


def _augment_order_with_headers(
    order: dict[tuple[int, str], list[tuple[float, float, dict]]],
    groups: list[dict],
    doc: fitz.Document,
    start: int,
    end: int,
    page_scan: dict[int, dict[str, Any]] | None = None,
) -> None:
    """레코드가 하나도 없는(표 없이 주석만 있는 서술형) 그룹은 _group_reading_order
    가 전혀 못 잡는다 — 그 함수는 오직 records 의 bbox 로만 그룹의 문서상 위치를
    세우기 때문이다. 그러면 그런 그룹의 주석이 owner 산정에서 통째로 빠져,
    앞선(엉뚱한) 그룹에 잘못 배정된다(review_round1 N8-scope-suspect — 2025H2
    p82~84 "ND10* 본선 시설공" 연속 서술형 그룹들에서 실측). 이 쪽·단에 실제로
    있는 ■ 머리글 y 위치를 이 파일 스스로(_group_ys, extract.py 코드 재사용 아님)
    다시 찾아, 그 쪽·단에 속한 그룹들과 문서 생성 순서대로 짝짓는다(개수가 같을
    때만 — 안 맞으면 잘못 짝지을 위험이 있으니 보강하지 않고 그대로 둔다)."""
    groups_by_key: dict[tuple[int, str], list[dict]] = {}
    for g in groups:
        pno = g.get("pdf_page")
        if pno is None:
            continue
        key = (int(pno), g.get("page_half") or "C")
        groups_by_key.setdefault(key, []).append(g)
    for (pno, ph), glist in groups_by_key.items():
        if pno > end:
            continue
        # K2: pno < start 머리글도 타임라인에 올린다(범위 시작 직전 그룹의
        # 범위 안 이어짐이 owner=None 으로 새지 않게).
        page = doc[pno - 1]
        scanned_words = (page_scan or {}).get(pno, {}).get("words")
        words = scanned_words if scanned_words is not None else _words(page)
        hx0 = hx1 = None
        for _p, x0, x1 in _halves(page):
            if _p == ph:
                hx0, hx1 = x0, x1
        if hx0 is None:
            continue
        gys = _group_ys(words, hx0, hx1)
        if len(gys) != len(glist):
            continue
        rows = order.setdefault((pno, ph), [])
        by_id = {id(g): i for i, (_, _, g) in enumerate(rows)}
        for y, g in zip(gys, glist):
            i = by_id.get(id(g))
            if i is None:
                rows.append((y, y, g))
                by_id[id(g)] = len(rows) - 1
            else:
                # 이미 레코드로 위치가 있는 그룹이라도, 소제목처럼 머리글과 첫
                # 레코드 사이(가격 없는 줄)에 있는 내용은 레코드 bbox 만으로는
                # 안 잡힌다 — 머리글 y 가 더 이르면 시작점을 그리로 당겨준다.
                y0, y1, gg = rows[i]
                rows[i] = (min(y0, y), y1, gg)
        rows.sort(key=lambda t: t[0])


def _note_word_gate(
    pdf_path: str | Path,
    result: dict[str, Any],
    pages: tuple[int, int] | None,
    table_cache: dict[int, list] | None = None,
    doc: fitz.Document | None = None,
    page_scan: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    groups = result.get("groups") or []
    records = result.get("records") or []
    subheaders = result.get("subheaders") or []
    pdf_path = Path(pdf_path)
    own_doc = doc is None
    if doc is None:
        doc = fitz.open(pdf_path)
    start, end = (1, doc.page_count) if pages is None else pages
    start = max(1, start)
    end = min(doc.page_count, end)

    order = _group_reading_order(groups, records)
    _augment_order_with_headers(order, groups, doc, start, end, page_scan=page_scan)
    # N8-scope: 쪽·단을 넘어 이어지는 그룹(표가 이전 쪽에서 시작해 이 쪽까지
    # 이어지는 경우)의 소유권을 (쪽,단) 키 하나만으로는 못 잡는다 — 이 쪽·단에
    # 이 그룹 자신의 표 조각이 아직 없으면(표 다음 조각이 이 쪽 더 아래에서
    # 시작하면) "이 쪽·단의 첫 표보다 앞선 그룹" 폴백이 문서 생성 순서상
    # 바로 앞 그룹(엉뚱한 이웃)을 집었다(review_round1 실측: 2025H2 p38→39
    # "AE22* 주형보" 그룹의 주석이 이전 그룹 "AE18* 잭"으로 샘). 전체 문서를
    # (쪽, 단, y0) 하나의 순서로 이은 타임라인을 만들어, 쪽을 넘는 연속도
    # 올바로 잡는다.
    global_timeline: list[tuple[tuple[int, int, float], dict]] = []
    for (og_pno, og_ph), og_rows in order.items():
        hr = _HALF_RANK.get(og_ph, 0)
        for y0, _y1, g in og_rows:
            global_timeline.append(((og_pno, hr, y0), g))
    global_timeline.sort(key=lambda t: t[0])
    timeline_keys = [t[0] for t in global_timeline]
    dest_cache: dict[Any, tuple[str, str, str, str]] = {}
    # G02i2 F5 gate_false_pass 보강(아래 has_note_signal 이어짐 판정용): 그룹의
    # "정답" 낱말열(원문 그대로, item/item_raw)을 미리 다 만들어 둔다.
    gmap_all: dict[Any, dict] = {g.get("group_id"): g for g in groups}
    # N8-scope: 소제목(subheaders.jsonl, 예: "ND109.261* 메사쉴드 갱내 자재…")은
    # 표 안에 있지만 notes 항목·표·그림 어디에도 없다 — _table_bodies() 가 소제목
    # 줄을 표 몸통에서 빠뜨리면(review_round1 실측: 2025H2 p84) 엉뚱하게 note
    # band 로 새어 들어가 missing 으로 잡힌다. 그룹별 소제목 글줄도 네 번째
    # 목적지로 인정한다.
    subs_by_group: dict[Any, str] = {}
    for sub in subheaders:
        gid = sub.get("group_id")
        blob = f"{sub.get('code_pattern') or ''}\n{sub.get('text') or ''}"
        subs_by_group[gid] = subs_by_group.get(gid, "") + "\n" + blob

    def dest(g: dict) -> tuple[str, str, str, str]:
        gid = g.get("group_id")
        if gid not in dest_cache:
            dest_cache[gid] = _group_destinations(g)
        return dest_cache[gid]

    missing: list[dict[str, Any]] = []
    duplicate: list[dict[str, Any]] = []
    figure_text: list[dict[str, Any]] = []
    # G02i2 F5: notes_order_mismatch 용 — 그룹마다 "주석 영역에서 제외 대상
    # (도식·그림 제목·배너·표·머리글·쪽번호)을 뺀 원문 낱말열"을 읽기 순서
    # 그대로 모은다(missing/duplicate 판정과 정확히 같은 시점·같은 제외
    # 기준의 낱말 집합 — 아래 diagram/caption/structural 제외를 통과한 낱말).
    order_seq: dict[Any, list[str]] = {}
    gate_page_errors: list[dict[str, Any]] = []
    range_pages = (start, end)

    for pno in range(start, end + 1):
        page = doc[pno - 1]
        scanned = (page_scan or {}).get(pno)
        if scanned:
            words = scanned["words"]
            bodies = scanned["bodies"]
        else:
            words = _words(page)
            bodies = _table_bodies(page, words=words)
        image_boxes = _image_boxes(page, gate_page_errors)
        cached = table_cache.get(pno) if table_cache is not None else None
        page_tables = _page_tables(page, cached=cached, errors=gate_page_errors)
        legend_boxes = _diagram_legend_boxes(page, page_tables)
        note_subtables = _note_subtables(page, page_tables, legend_boxes)
        # G02i2 F5 gate_false_pass 보강(코치 독립 시험 T05, review 표본
        # 2024H1#p35#79/#80): 가로판형(좌우 2단) 쪽에서는 header_ys 를 반(L/R)
        # 구분 없이 페이지 전체로 하나의 목록에 합쳐 왔다 — 그러면 왼쪽 단의
        # 표 머리글 행 y 와 우연히(±9pt 안) 겹치는 오른쪽 단의 진짜 주석 낱말
        # (또는 그 반대)이 "표 머리글 위"로 잘못 보여 _skip_word() 가 지워
        # 버렸다("④본단가적용을위한…"의 "단가"가 반복적으로 사라짐, 4권
        # 전체 재현). 반마다 자기 반의 머리글 행만 보게 나눈다.
        header_ys_by_half: dict[str, list[float]] = {}
        for _ph, hx0, hx1 in _halves(page):
            header_ys_by_half[_ph] = _header_ys(words, hx0, hx1)
        bands = _note_bands(page, bodies)
        if not bands:
            continue

        for ph, hx0, hx1, by0, by1 in bands:
            band_words_pre = [
                w
                for w in words
                if hx0 - 1 <= (w[0] + w[2]) / 2 < hx1 + 1 and by0 < (w[1] + w[3]) / 2 < by1
            ]
            # N8-scope: 표 사이 빈 구간이 전부 【단가정의】 주석이라고 가정하지
            # 않는다(review_round1 N8-scope-suspect·notes_missing 대량 오탐 —
            # 표지·목차·표준시장단가 색인 목록·"- 굴진 총연장 150m이하" 같은 표
            # 소구간 표제가 표와 표 사이 빈틈에 있으면 전부 엉뚱한 그룹의 주석
            # 누락으로 잘못 잡혔다). 이 구간에 실제 주석 신호(【단가정의】 라벨
            # 또는 원문자 ①…)가 하나도 없으면 애초에 주석 구간으로 보지 않는다.
            has_note_signal = any(
                "단가정의" in w[4].replace(" ", "")
                or w[4].strip()[:1] in CIRCLED_CHARS
                or w[4].strip().startswith("※")
                for w in band_words_pre
            )
            hr = _HALF_RANK.get(ph, 0)

            def _owner_at(y: float) -> dict | None:
                # 이 (쪽,단,y) 위치가 어느 그룹의 주석인지: 문서 전체를 (쪽, 단,
                # y0) 하나의 순서로 이은 타임라인에서, 이 위치보다 앞서(또는 이
                # 위치에서) 시작한 그룹 중 가장 나중 것을 owner 로 본다.
                # review_round1 N8-scope-suspect: (쪽,단) 키 안에서만 비교하면,
                # 그룹의 표가 쪽을 넘어 이어질 때(예: 2025H2 p38 "AE22* 주형보"
                # 그룹의 표가 p38→p39 로 이어짐) 그 그룹의 표 다음 조각이 아직
                # 없는 쪽 앞머리에서는 "문서 생성 순서상 바로 앞 그룹"(엉뚱한
                # 이웃)으로 새어나갔다. 전체 타임라인 하나로 쪽 경계를 없앴다.
                cur_key = (pno, hr, y + 1.0)
                i = bisect.bisect_right(timeline_keys, cur_key)
                if i == 0:
                    return None
                return global_timeline[i - 1][1]

            if not has_note_signal:
                # K1: 밴드를 건너뛸지는 산출(_group_output_chars)과 무관한 원문
                # 신호로만 정한다. 이 밴드가 다음 ■ 머리글·대분류 배너·표 시작·
                # 분야 표지 뒤에 있으면 owner 없음. 그 밖의 신호 없는 밴드는
                # 직전 owner 의 이어짐 — 단, 원문에서 이미 그 owner 의 주석
                # 낱말을 모은 뒤에만(산출 prefix 가 아님).
                if _band_source_ends_owner(band_words_pre, by0, bodies, hx0, hx1):
                    continue
                has_src_ongoing = False
                for w in band_words_pre:
                    o = _owner_at((w[1] + w[3]) / 2.0)
                    gid = o.get("group_id") if o is not None else None
                    if gid is not None and order_seq.get(gid):
                        has_src_ongoing = True
                        break
                    # K2: 범위 시작 전 그룹이 타임라인에 있으면 범위 안 이어짐으로 본다.
                    if o is not None:
                        gpage = o.get("pdf_page")
                        if isinstance(gpage, int) and gpage < start:
                            has_src_ongoing = True
                            break
                if not has_src_ongoing:
                    continue

            band_words = _align_subtable_row_words(band_words_pre, note_subtables)
            for line in _cluster_word_lines(band_words):
                sorted_line = sorted(line, key=lambda w: w[0])
                line_yc = sum((w[1] + w[3]) / 2.0 for w in line) / len(line)
                owner = _owner_at(line_yc)
                if owner is None:
                    continue
                item_blob, table_blob, fig_blob, notes_only_blob = dest(owner)
                sub_blob = subs_by_group.get(owner.get("group_id"), "")
                line_txt = " ".join(w[4] for w in sorted_line)
                is_structural_line = _note_line_is_structural(line_txt)
                # G02i2 F5 gate_false_pass 보강(코치 독립 시험 T05/gate_false_pass
                # code_issues severity=medium): 표 안 소제목 행(예: 2025H2 p84
                # "ND109.261* 메사쉴드 갱내자재 소운반/철근,")은 _table_bodies()가
                # 항상 표 몸통으로 정확히 감싸주지 못해 표 사이 빈틈(note band)에
                # 새어 들어올 수 있다. 이런 줄은 subheaders.jsonl 에 이미 그 자체가
                # 표 안 소제목으로 기록돼 있고(missing/duplicate 판정에서는 dest()
                # 의 "subheader" 목적지로 이미 정상 처리됨), notes 항목·표·머리글
                # 어디에도 들어가지 않는 구조 표지이므로 order_seq(순서 대조)에서도
                # 애초에 뺀다 — 줄 전체 낱말이 이 그룹의 소제목 blob 안에 전부
                # 있을 때만(부분 우연 일치 방지) 소제목 행으로 본다.
                if not is_structural_line and sub_blob.strip():
                    line_words_txt = [w[4] for w in sorted_line if w[4].strip()]
                    if line_words_txt and all(_word_in_text(t, sub_blob) for t in line_words_txt):
                        is_structural_line = True
                # review_round3 code_issues(severity=high): "그림 제목(예: "[그림-1]
                # 금속사다리 표준상세도면")의 낱말이 다른 곳(머리글·본문 문장)에도
                # 우연히 같은 문자열로 나오면(예: "사다리") blob 부분일치만으로는
                # 두 물리적 위치를 구분 못해 진짜 그림 제목 줄까지 "중복"으로
                # 잘못 잡았다(4권 전체 재현, notes_duplicate 가 항상 1). 캡션 줄
                # 자체는 그 물리적 위치가 곧 "figure" 목적지이므로, 이 줄의 낱말은
                # item/table 목적지와 겹쳐도 중복 판정에서 제외한다.
                is_caption_line = (not is_structural_line) and line_txt.lstrip().startswith(("[그림", "[표준도]"))  # 코치 핫픽스 09-17: 표준도 제목 줄
                # G02i2 2차(코치 재검, 2024H1 p45#107 ②): 진짜 작은 표 칸 안
                # 짧은 괄호 문구("(견고하고 미려한 시공이 요구되는 경우)")가
                # 예시도의 짧은 괄호 설명 규칙(20자 이하 괄호줄)과 우연히
                # 겹쳐 그림으로 잘못 잡혔다 — 이미 note_subtables 로 이 줄이
                # 진짜 표 칸 안에 있는 걸 알면, 도식 줄 판정 자체를 건너뛴다
                # (표 칸 글자는 정의상 도식이 아니다).
                in_subtable_cell = any(
                    cx0 - 1 <= (w[0] + w[2]) / 2 <= cx1 + 1 and cy0 - 1 <= (w[1] + w[3]) / 2 <= cy1 + 1
                    for w in sorted_line
                    for table_rows in note_subtables
                    for row in table_rows
                    for cx0, cy0, cx1, cy1 in row
                )
                is_diagram_line = (
                    not is_structural_line
                    and not is_caption_line
                    and not in_subtable_cell
                    and _line_has_diagram_signal(sorted_line, image_boxes)
                )
                # G02i2 F5: order_seq(notes_order_mismatch 용) 는 읽기 순서가
                # 생명이라 x 로 정렬된 sorted_line 을 써야 한다 — 정렬 안 된
                # line 을 쓰면 한 줄 안에서도 낱말이 뒤섞여(PyMuPDF words 원본
                # 순서가 항상 좌→우는 아니다) 모든 그룹에서 order_seq 가 깨졌다.
                for w in sorted_line:
                    wx0, wy0, wx1, wy1, text = w
                    if is_structural_line:
                        continue
                    yc = (wy0 + wy1) / 2.0
                    if _skip_word(text, yc, header_ys_by_half.get(ph, []), page.rect.height):
                        continue
                    # G02i2 F5: order_seq(원문 낱말열, 읽기순서) 는 도식/그림
                    # 낱말만 빼고 나머지는 다 모은다 — 아래 len(wn)<2 길이
                    # 거름(missing/duplicate 판정만을 위한 노이즈 제거)보다
                    # 먼저 결정해야, 1글자 낱말("및"·"중"·"시" 등 조사·부사)이
                    # order_seq 에서까지 빠져 거짓 불일치로 잡히지 않는다.
                    xc, yc2 = (wx0 + wx1) / 2.0, (wy0 + wy1) / 2.0
                    in_legend_box = any(
                        bx0 - 1 <= xc <= bx1 + 1 and by0b - 1 <= yc2 <= by1b + 1
                        for bx0, by0b, bx1, by1b in legend_boxes
                    )
                    is_diagram_word = (
                        not is_caption_line
                        and (
                            in_legend_box
                            or is_diagram_line
                            or _is_figure_word(
                                wx0, wy0, wx1, wy1, text, image_boxes, len(sorted_line) == 1
                            )
                        )
                    )
                    # G02i2 F5 gate_false_pass 보강(코치 독립 시험 T05, review
                    # 표본 2024H1#p84#212 "[그림-1] 옥상 신축조인트 시공절차"):
                    # 캡션 줄(is_caption_line)은 is_diagram_word 판정에서
                    # "not is_caption_line" 으로 앞서 걸러져 (in_legend_box·
                    # is_diagram_line 등을 볼 것도 없이) is_diagram_word 가
                    # 늘 False 가 됐다 — 그 결과 "도식 낱말이 아니다" 로
                    # 잘못 해석돼 order_seq 에 그대로 들어갔다. 그림 제목은
                    # notes 가 아니라 figures 로 가는 목적지라 _group_output_
                    # chars() 의 out 에는 애초에 없으므로, order_seq 에서도
                    # 캡션 줄은 별도로 뺀다(missing/duplicate 판정의 "figure"
                    # 목적지 특례는 안 건드린다).
                    if not is_diagram_word and not is_caption_line:
                        order_seq.setdefault(owner.get("group_id"), []).append(text)
                    wn = _compact(text)
                    if len(wn) < 2:
                        continue
                    if is_diagram_word:
                        figure_text.append(
                            {
                                "pdf_page": pno,
                                "text": text,
                                "x": round((wx0 + wx1) / 2, 1),
                                "y": round((wy0 + wy1) / 2, 1),
                            }
                        )
                        # review_round3 gate_false_pass(실험2) 시도·되돌림: 이 낱말이
                        # notes_only_blob(item 문장)에도 나오면 duplicate 로 잡아보는
                        # 시도를 했으나, 실측(4권 dev 채점, 2025H2 p46~95)에서 60건이
                        # 새로 잡혔고 그중 다수가 "5m"·"100m"·"할증"·"NA209.11120"처럼
                        # 도식 라벨과 같은 글자가 같은 그룹의 진짜 본문 문장에 정상
                        # 적으로도 나오는 우연한 겹침이었다(예: 2025H2 p89~90 item④
                        # "NA209.11120 및 NA209.11220 단가의…할증은…" 문장 자체가 표
                        # 안 범례 코드·"할증"과 글자가 같다) — 바로 위 "사다리" 예외를
                        # 만들게 한 것과 같은 blob 부분일치 한계가 여기서도 재현돼
                        # notes_duplicate 를 0으로 유지하지 못했다(N8 pass 조건 위반).
                        # exp2(도식 글자가 item 에 실제로 섞이는 회귀)는 여전히 이
                        # 게이트가 못 잡는 사각지대로 남지만, 오탐이 실측 산출을
                        # 훨씬 크게 해치므로 이 낱말 단위 교차검증은 넣지 않는다
                        # (review_round3 code_issues 로 문서화만 하고 되돌림).
                        continue
                    # review_round3 code_issues(severity=high)/N8-gate-pass: "figure"
                    # 목적지는 이 낱말의 줄이 실제 그림 제목 줄([그림-N] 로 시작)일
                    # 때만 검사한다. 아니면 흔한 복합어("금속사다리"의 "사다리" 등)가
                    # 본문 다른 자리에도 우연히 같은 글자로 있다는 이유만으로, 이
                    # 물리적 위치와 무관한 그림 제목 blob 과 겹쳐 잡혀 진짜 item
                    # 문장까지 "중복"으로 잘못 세었다(4권 전체 재현, notes_duplicate
                    # 가 항상 1이던 원인).
                    hits = [
                        dst
                        for dst, blob in (
                            ("item", item_blob),
                            ("table", table_blob),
                            ("figure", fig_blob if is_caption_line else ""),
                            ("subheader", sub_blob),
                        )
                        if _word_in_text(text, blob)
                    ]
                    item = {
                        "pdf_page": pno,
                        "group_id": owner.get("group_id"),
                        "text": text,
                        "x": round((wx0 + wx1) / 2, 1),
                        "y": round((wy0 + wy1) / 2, 1),
                    }
                    if len(hits) == 0:
                        missing.append(item)
                    elif is_caption_line:
                        # 그림 제목 줄 자체: item·표 텍스트에 같은 낱말이 우연히
                        # 있어도(위 주석 참고) 이 물리적 위치의 목적지는 그림
                        # 제목이므로 중복으로 세지 않는다.
                        pass
                    elif "figure" in hits and len(hits) > 1:
                        # item·표 사이의 겹침(설명 글과 표가 같은 내용을 되풀이하는
                        # 정상적인 경우)은 중복으로 보지 않는다 — 그림 제목과
                        # 겹치는 경우만 진짜 오분류(중복)로 본다. 다만 "item" 이
                        # 실제 주석 문장이 아니라 그룹 머리글·대분류명(예: "■JB******
                        # 금속사다리")에서만 온 것이면, 그림 제목이 그 낱말(예:
                        # "사다리")을 그대로 다시 쓰는 자연스러운 반복일 뿐이라
                        # 중복으로 보지 않는다(4권 전체 재현 — 실제 오분류가 아님).
                        if "item" in hits and not _word_in_text(text, notes_only_blob):
                            pass
                        else:
                            duplicate.append(dict(item, keys=hits))
    if own_doc:
        doc.close()

    # G02i2 F5: notes_order_mismatch — 그룹마다 "원문 낱말열(읽기순서, 위에서
    # 도식·그림제목·배너·표머리글·쪽번호를 뺀 것)"과 "항목·주석표 텍스트를
    # 순서대로 이은 낱말열(산출)"을 문자 단위로 맞대본다. 완전 일치가
    # 아니면(끝조각 삭제(A)·라벨 삽입(B)·항목 삽입(C)·배너 유입(D)·순서
    # 뒤바뀜(E) 무엇이든) 첫 불일치 자리를 잡는다 — 기존 missing/duplicate
    # 는 낱말이 "어딘가에 있는지"만 보고 순서를 안 봐서 이런 변조를 못 잡는다.
    gmap_all = {g.get("group_id"): g for g in groups}
    order_mismatch: list[dict[str, Any]] = []
    for gid, words_seq in order_seq.items():
        g = gmap_all.get(gid)
        if g is None:
            continue
        src = "".join(_compact(w) for w in words_seq)
        out = _group_output_chars(g, pages=range_pages)
        if not src or src == out:
            continue
        sm = difflib.SequenceMatcher(None, src, out, autojunk=False)
        pos = None
        for tag, i1, _i2, j1, _j2 in sm.get_opcodes():
            if tag != "equal":
                pos = (i1, j1)
                break
        if pos is None:
            continue
        i1, j1 = pos
        order_mismatch.append(
            {
                "group_id": gid,
                "src_pos": i1,
                "out_pos": j1,
                "src_context": src[max(0, i1 - 15) : i1 + 25],
                "out_context": out[max(0, j1 - 15) : j1 + 25],
            }
        )

    # 주석이 있는데 원문 낱말열이 하나도 모이지 않은 그룹은 "검사 안 됨".
    # K2: 범위 밖에서 시작한 그룹도 범위 안 이어짐이 있으면 검사한다.
    # owner 를 못 잡으면 0 으로 넘기지 않고 불일치로 센다.
    last_pre = None
    pre_range = [
        g
        for g in groups
        if isinstance(g.get("pdf_page"), int) and g["pdf_page"] < start
    ]
    if pre_range:
        last_pre = max(
            pre_range,
            key=lambda g: (int(g["pdf_page"]), _HALF_RANK.get(g.get("page_half") or "C", 0)),
        )
    for g in groups:
        gid = g.get("group_id")
        if "".join(_compact(w) for w in order_seq.get(gid, [])):
            continue
        out_in_range = _group_output_chars(g, pages=range_pages)
        note_pages = [n.get("pdf_page") for n in (g.get("notes") or []) if isinstance(n.get("pdf_page"), int)]
        in_range_notes = [p for p in note_pages if start <= p <= end]
        should_check = bool(in_range_notes) or bool(out_in_range)
        if not should_check and last_pre is not None and g is last_pre:
            should_check = True
        if not should_check:
            continue
        out = out_in_range or _group_output_chars(g)
        if not out and not should_check:
            continue
        order_mismatch.append(
            {
                "group_id": gid,
                "src_pos": 0,
                "out_pos": 0,
                "src_context": "",
                "out_context": (out or "")[:40],
                "reason": "unchecked: no source note words collected for this group",
            }
        )

    return {
        "notes_missing": missing,
        "notes_duplicate": duplicate,
        "notes_figure_text": figure_text,
        "notes_order_mismatch": order_mismatch,
        "gate_page_errors": gate_page_errors,
    }


def check_conservation(
    pdf_path: str | Path,
    result: dict[str, Any],
    pages: tuple[int, int] | None = None,
    table_cache: dict[int, list] | None = None,
) -> dict[str, Any]:
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    start, end = (1, doc.page_count) if pages is None else pages
    start = max(1, start)
    end = min(doc.page_count, end)

    recs_by_page: dict[int, list[dict]] = {}
    rec_halves: set[tuple[int, str]] = set()
    recorded: set[tuple[str, int]] = set()
    for rec in result.get("records") or []:
        recs_by_page.setdefault(int(rec["pdf_page"]), []).append(rec)
        rec_halves.add((int(rec["pdf_page"]), rec.get("page_half") or "C"))
        recorded.add((str(rec.get("code") or ""), int(rec["pdf_page"])))
    subs_by_page: dict[int, list[dict]] = {}
    for sub in result.get("subheaders") or []:
        subs_by_page.setdefault(int(sub["pdf_page"]), []).append(sub)

    page_reports: list[dict[str, Any]] = []
    tot_body = tot_assigned = tot_missing = tot_dup = tot_split = 0
    tot_lost = tot_ins = tot_unrec = 0
    split_all: list[dict[str, Any]] = []
    lost_all: list[dict[str, Any]] = []
    inserted_all: list[dict[str, Any]] = []
    digit_only_all: list[dict[str, Any]] = []
    unrec_all: list[dict[str, Any]] = []

    in_note = False
    if start > 1:
        for pno in range(1, start):
            page = doc[pno - 1]
            words_pre = _words(page)
            for _ph, hx0, hx1 in _halves(page):
                in_note = _end_in_note(_note_events(words_pre, hx0, hx1), in_note)

    page_scan: dict[int, dict[str, Any]] = {}
    for pno in range(start, end + 1):
        page = doc[pno - 1]
        words = _words(page)
        drawings = page.get_drawings()
        hlines = _wide_h(page, drawings)
        vlines = _vlines(page, drawings)
        bodies = _table_bodies(page, words=words, hlines=hlines)
        page_scan[pno] = {"words": words, "bodies": bodies}
        # G02i2 F5 gate_false_pass 보강과 같은 이유(반을 안 나누면 우연히 다른
        # 반의 머리글 행 y 와 겹치는 표 본문 낱말이 잘못 걸러진다) — 이 기존
        # 게이트도 반별로 나눠 적용한다.
        halves_page = _halves(page)
        header_ys_by_half: dict[str, list[float]] = {
            _ph: _header_ys(words, hx0, hx1) for _ph, hx0, hx1 in halves_page
        }

        def _half_of(xc: float) -> str:
            for _ph, hx0, hx1 in halves_page:
                if hx0 - 1 <= xc < hx1 + 1:
                    return _ph
            return halves_page[0][0] if halves_page else "C"

        body_words = []
        for w in words:
            xc, yc = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            if _skip_word(w[4], yc, header_ys_by_half.get(_half_of(xc), []), page.rect.height):
                continue
            if any(b[0] <= xc <= b[2] and b[1] < yc < b[3] for b in bodies):
                body_words.append(w)

        recs = recs_by_page.get(pno, [])
        subs = subs_by_page.get(pno, [])
        missing: list[dict[str, Any]] = []
        duplicate: list[dict[str, Any]] = []
        split_words: list[dict[str, Any]] = []
        assigned = 0
        for w in body_words:
            hits = _assign(w, recs, subs)
            uniq = list(dict.fromkeys(hits))
            if len(uniq) == 1:
                assigned += 1
            elif len(uniq) == 0:
                missing.append(
                    {
                        "text": w[4],
                        "x": round((w[0] + w[2]) / 2, 1),
                        "y": round((w[1] + w[3]) / 2, 1),
                    }
                )
            else:
                duplicate.append(
                    {
                        "text": w[4],
                        "x": round((w[0] + w[2]) / 2, 1),
                        "y": round((w[1] + w[3]) / 2, 1),
                        "keys": uniq,
                    }
                )
            hits_strict = _assign(w, recs, subs, allow_digit=False)
            if hits and not hits_strict:
                digit_only_all.append(
                    {
                        "pdf_page": pno,
                        "text": w[4],
                        "x": round((w[0] + w[2]) / 2, 1),
                        "y": round((w[1] + w[3]) / 2, 1),
                        "keys": list(dict.fromkeys(hits)),
                    }
                )

        for w in words:
            wx0, wy0, wx1, wy1, text = w
            wn = _compact(text)
            if len(wn) < 2:
                continue
            xc, yc = (wx0 + wx1) / 2.0, (wy0 + wy1) / 2.0
            hosts = []
            for rec in recs:
                bbox = rec.get("bbox") or [0, 0, 0, 0]
                if len(bbox) != 4:
                    continue
                bx0, by0, bx1, by1 = (float(v) for v in bbox)
                if by0 - 0.4 <= yc < by1 + 0.4 and bx0 - 1.0 <= xc <= bx1 + 1.0:
                    hosts.append(rec)
            if not hosts:
                continue
            if any(_wholly_in_one_field(text, rec) for rec in hosts):
                continue
            if _vline_splits_word(vlines, wx0, wy0, wx1, wy1):
                continue
            overlapped_ok = False
            for rec in hosts:
                rb = rec.get("bbox") or [0, 0, 0, 0]
                for other in recs:
                    if other is rec:
                        continue
                    if not _bbox_overlap(rb, other.get("bbox") or [0, 0, 0, 0]):
                        continue
                    if _wholly_in_one_field(text, other):
                        overlapped_ok = True
                        break
                if overlapped_ok:
                    break
            if overlapped_ok:
                continue
            host = hosts[0]
            item = {
                "code": host.get("code"),
                "pdf_page": pno,
                "word": text,
                "name": host.get("name"),
                "spec": host.get("spec"),
                "unit": host.get("unit"),
            }
            split_words.append(item)
            split_all.append(item)

        lost_spaces: list[dict[str, Any]] = []
        inserted_spaces: list[dict[str, Any]] = []
        for rec in recs:
            bbox = rec.get("bbox") or [0, 0, 0, 0]
            if len(bbox) != 4:
                continue
            bx0, by0, bx1, by1 = (float(v) for v in bbox)
            fields = _raw_field_values(rec)
            rec_words: list[tuple[float, float, float, float, str]] = []
            for w in words:
                wx0, wy0, wx1, wy1, text = w
                xc, yc = (wx0 + wx1) / 2.0, (wy0 + wy1) / 2.0
                if not (by0 - 0.4 <= yc < by1 + 0.4 and bx0 - 1.0 <= xc <= bx1 + 1.0):
                    continue
                if not any(_word_in_text(text, f, allow_digit=False) for f in fields):
                    continue
                rec_words.append(w)
                if len(_compact(text)) < 2:
                    continue
                for field in fields:
                    if _inserted_space(text, field):
                        item_ins = {
                            "code": rec.get("code"),
                            "pdf_page": pno,
                            "word1": text,
                            "word2": "",
                            "field": field,
                        }
                        inserted_spaces.append(item_ins)
                        inserted_all.append(item_ins)
                        break
            for line in _cluster_word_lines(rec_words):
                line = sorted(line, key=lambda w: w[0])
                for i in range(len(line) - 1):
                    a, b = line[i], line[i + 1]
                    if _vline_between(
                        vlines,
                        a[2],
                        b[0],
                        min(a[1], b[1]),
                        max(a[3], b[3]),
                    ):
                        continue
                    t1, t2 = a[4], b[4]
                    glued = t1 + t2
                    for field in fields:
                        if glued in field:
                            item_lost = {
                                "code": rec.get("code"),
                                "pdf_page": pno,
                                "word1": t1,
                                "word2": t2,
                                "field": field,
                            }
                            lost_spaces.append(item_lost)
                            lost_all.append(item_lost)
                            break

        unrecorded: list[dict[str, Any]] = []
        for ph, hx0, hx1 in _halves(page):
            ev = _note_events(words, hx0, hx1)
            if (pno, ph) in rec_halves:
                for code, yc, txt in _line_start_codes(words, hx0, hx1):
                    if _in_note_at(yc, ev, in_note):
                        continue
                    if (code, pno) in recorded:
                        continue
                    item_u = {
                        "code": code,
                        "pdf_page": pno,
                        "page_half": ph,
                        "line": txt,
                    }
                    unrecorded.append(item_u)
                    unrec_all.append(item_u)
            in_note = _end_in_note(ev, in_note)

        page_reports.append(
            {
                "pdf_page": pno,
                "body_words": len(body_words),
                "assigned": assigned,
                "missing_count": len(missing),
                "duplicate_count": len(duplicate),
                "split_words_count": len(split_words),
                "lost_spaces_count": len(lost_spaces),
                "inserted_spaces_count": len(inserted_spaces),
                "unrecorded_codes_count": len(unrecorded),
                "missing": missing,
                "duplicate": duplicate,
                "split_words": split_words,
                "lost_spaces": lost_spaces,
                "inserted_spaces": inserted_spaces,
                "unrecorded_codes": unrecorded,
            }
        )
        tot_body += len(body_words)
        tot_assigned += assigned
        tot_missing += len(missing)
        tot_dup += len(duplicate)
        tot_split += len(split_words)
        tot_lost += len(lost_spaces)
        tot_ins += len(inserted_spaces)
        tot_unrec += len(unrecorded)

    # N8: 주석 게이트. 같은 doc·쪽 스캔(words/bodies)을 재사용한다.
    note_gate = _note_word_gate(
        pdf_path, result, pages, table_cache=table_cache, doc=doc, page_scan=page_scan
    )
    doc.close()

    # H2: 원문 낱말 -> raw 필드뿐 아니라 최종 필드(name·spec·price·labor_ratio·unit_norm)
    # 자체의 불변식도 게이트에 넣는다.
    gates = _field_gates(result.get("records") or [], result.get("subheaders") or [])
    n_empty_name = len(gates["empty_name"])
    n_unresolved_inherit = len(gates["unresolved_inherit"])
    n_parse_mismatch = len(gates["parse_mismatch"])
    n_inherit_mismatch = len(gates["inherit_mismatch"])
    n_notes_missing = len(note_gate["notes_missing"])
    n_notes_duplicate = len(note_gate["notes_duplicate"])
    n_notes_figure_text = len(note_gate["notes_figure_text"])
    n_notes_order_mismatch = len(note_gate["notes_order_mismatch"])
    n_gate_page_errors = len(note_gate.get("gate_page_errors") or [])

    # G02i2 F5: pua_chars — records·subheaders·groups 어디에도 사용자 정의
    # 영역(PUA) 글자가 남아 있으면 안 된다(pass 조건).
    pua_hits = _pua_scan(result)
    n_pua_chars = len(pua_hits)

    return {
        "half": result.get("half"),
        "pages": page_reports,
        "split_words": split_all,
        "lost_spaces": lost_all,
        "inserted_spaces": inserted_all,
        "unrecorded_codes": unrec_all,
        "digit_only": digit_only_all,
        "empty_name": gates["empty_name"],
        "unresolved_inherit": gates["unresolved_inherit"],
        "parse_mismatch": gates["parse_mismatch"],
        "inherit_mismatch": gates["inherit_mismatch"],
        "notes_missing": note_gate["notes_missing"],
        "notes_duplicate": note_gate["notes_duplicate"],
        "notes_figure_text": note_gate["notes_figure_text"],
        "notes_order_mismatch": note_gate["notes_order_mismatch"],
        "pua_chars": pua_hits,
        "totals": {
            "body_words": tot_body,
            "assigned": tot_assigned,
            "missing": tot_missing,
            "duplicate": tot_dup,
            "split_words": tot_split,
            "lost_spaces": tot_lost,
            "inserted_spaces": tot_ins,
            "unrecorded_codes": tot_unrec,
            "digit_only": len(digit_only_all),
            "empty_name": n_empty_name,
            "unresolved_inherit": n_unresolved_inherit,
            "parse_mismatch": n_parse_mismatch,
            "inherit_mismatch": n_inherit_mismatch,
            "notes_missing": n_notes_missing,
            "notes_duplicate": n_notes_duplicate,
            "notes_figure_text": n_notes_figure_text,
            "notes_order_mismatch": n_notes_order_mismatch,
            "pua_chars": n_pua_chars,
            "gate_page_errors": n_gate_page_errors,
            "pass": tot_missing == 0
            and tot_dup == 0
            and tot_split == 0
            and tot_lost == 0
            and tot_ins == 0
            and tot_unrec == 0
            and n_empty_name == 0
            and n_unresolved_inherit == 0
            and n_parse_mismatch == 0
            and n_inherit_mismatch == 0
            and n_notes_missing == 0
            and n_notes_duplicate == 0
            and n_notes_order_mismatch == 0
            and n_pua_chars == 0
            and n_gate_page_errors == 0,
        },
    }
