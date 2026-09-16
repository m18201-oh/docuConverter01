"""표 본문 낱말 보존 검사.

추출 코드(extract.py)의 함수를 재사용하지 않는다.
낱말은 page.get_text("words") 원자료에서 직접 모은다.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pymupdf as fitz

CODE_RE = re.compile(r"[A-Z]{2}\d{3}\.\d{5}")
CODE_FULL = re.compile(r"^[A-Z]{2}\d{3}\.\d{5}$")
STAR_RE = re.compile(r"[A-Z]{2}\d{3}\.\d+\*")
PRICE_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$|^\d+$")
LABOR_RE = re.compile(r"^\d+(?:\.\d+)?%$")
HALF_LABOR_RE = re.compile(r"^[‘'′`]?\d{2}[상하]")
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


def _wide_h(page: fitz.Page) -> list[tuple[float, float, float]]:
    """(x0, x1, y) 가로 벡터 선. 짧은 선은 버린다."""
    out: list[tuple[float, float, float]] = []
    for d in page.get_drawings():
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


def _vlines(page: fitz.Page) -> list[tuple[float, float, float]]:
    """(x, y0, y1) 세로 벡터 선. 1행 표 ~19.8pt 도 포함한다."""
    out: list[tuple[float, float, float]] = []
    for d in page.get_drawings():
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
    return HEADER_NORM.get(text.strip().replace(" ", ""))


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


def _words(page: fitz.Page) -> list[tuple[float, float, float, float, str]]:
    rows = []
    for w in page.get_text("words"):
        t = w[4]
        if t is None or t == "":
            continue
        rows.append((float(w[0]), float(w[1]), float(w[2]), float(w[3]), t))
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


def _table_bodies(page: fitz.Page) -> list[tuple[float, float, float, float]]:
    """본문 영역 사각형 (x0,y0,x1,y1). 헤더 행 아래 ~ 표 바닥."""
    words = _words(page)
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
    if _header_field(text) is not None and any(abs(yc - hy) <= 9 for hy in header_ys):
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


def check_conservation(
    pdf_path: str | Path,
    result: dict[str, Any],
    pages: tuple[int, int] | None = None,
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

    for pno in range(start, end + 1):
        page = doc[pno - 1]
        words = _words(page)
        bodies = _table_bodies(page)
        vlines = _vlines(page)
        header_ys: list[float] = []
        for _ph, hx0, hx1 in _halves(page):
            header_ys.extend(_header_ys(words, hx0, hx1))

        body_words = []
        for w in words:
            xc, yc = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            if _skip_word(w[4], yc, header_ys, page.rect.height):
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

    doc.close()
    return {
        "half": result.get("half"),
        "pages": page_reports,
        "split_words": split_all,
        "lost_spaces": lost_all,
        "inserted_spaces": inserted_all,
        "unrecorded_codes": unrec_all,
        "digit_only": digit_only_all,
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
            "pass": tot_missing == 0
            and tot_dup == 0
            and tot_split == 0
            and tot_lost == 0
            and tot_ins == 0
            and tot_unrec == 0,
        },
    }
