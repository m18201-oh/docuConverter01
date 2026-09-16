"""PDF(born-digital 표)를 좌표로 읽어 레코드/그룹/소제목/쪽을 뽑는다.

표 행은 find_tables 의 row.cells 가 아니라 코드 낱말 y 중간점으로 나눈다.
열 x 구간은 헤더 문자열 + 표 격자(세로선)로 잡는다.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz

CODE_FIND = re.compile(r"[A-Z]{2}\d{3}\.\d{5}")
STAR_FIND = re.compile(r"[A-Z]{2}\d{3}\.\d+\*")
CODE_RE = CODE_FIND
PRICE_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$|^\d+$")
LABOR_RE = re.compile(r"^\d+(?:\.\d+)?%$")
INHERIT_CHARS = set('"\'＂〃“”＇')
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
DEFAULT6 = ["code", "name", "spec", "unit", "price", "labor"]
DEFAULT7 = DEFAULT6 + ["remark"]
FIELD_RE = re.compile(r"([가-힣]+(?:및[가-힣]+)*)분야자체표준시장단가")
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
UNIT_NORM = {"주": "tree", "㎡": "m2", "㎥": "m3", "톤": "ton"}
BANNER_RE = re.compile(r"대분류\s*([A-Z])(?:\s*[,，]\s*([A-Z]))?")


@dataclass
class Span:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class LineSeg:
    a: float
    b: float
    c: float  # y for H, x for V; a-b is the other axis span


def _sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _page_spans(page: fitz.Page) -> list[Span]:
    out: list[Span] = []
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                t = s.get("text") or ""
                if not t.strip() and t != " ":
                    continue
                x0, y0, x1, y1 = s["bbox"]
                out.append(Span(float(x0), float(y0), float(x1), float(y1), t))
    return out


def _page_chars(page: fitz.Page) -> list[Span]:
    out: list[Span] = []
    rd = page.get_text("rawdict")
    for b in rd.get("blocks", []):
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                for c in s.get("chars") or []:
                    ch = c.get("c") or ""
                    if ch == "":
                        continue
                    x0, y0, x1, y1 = c["bbox"]
                    out.append(Span(float(x0), float(y0), float(x1), float(y1), ch))
    return out


def _page_lines(page: fitz.Page) -> tuple[list[LineSeg], list[LineSeg]]:
    hs: list[LineSeg] = []
    vs: list[LineSeg] = []
    for d in page.get_drawings():
        for item in d.get("items", []):
            if item[0] != "l":
                continue
            p1, p2 = item[1], item[2]
            if abs(p1.y - p2.y) < 0.8:
                x0, x1 = sorted((p1.x, p2.x))
                if x1 - x0 > 40:
                    hs.append(LineSeg(float(x0), float(x1), float((p1.y + p2.y) / 2)))
            elif abs(p1.x - p2.x) < 0.8:
                y0, y1 = sorted((p1.y, p2.y))
                if y1 - y0 > 20:
                    vs.append(LineSeg(float(y0), float(y1), float((p1.x + p2.x) / 2)))
    return hs, vs


def _halves(page: fitz.Page) -> tuple[str, list[tuple[str, float, float]]]:
    r = page.rect
    if r.width > r.height:
        mid = r.width / 2
        return "landscape_2up", [("L", 0.0, mid), ("R", mid, r.width)]
    return "portrait_1up", [("C", 0.0, r.width)]


def _in_half(sp: Span, x0: float, x1: float) -> bool:
    return x0 - 1 <= sp.xc < x1 + 1


def _cluster_xs(xs: list[float], tol: float = 1.8) -> list[float]:
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


def _join_line(spans: list[Span]) -> str:
    if not spans:
        return ""
    spans = sorted(spans, key=lambda s: s.x0)
    parts: list[str] = []
    prev: Span | None = None
    for s in spans:
        t = s.text
        if prev is None:
            parts.append(t)
        else:
            gap = s.x0 - prev.x1
            if gap < 2.2:
                parts[-1] += t
            else:
                parts.append(t)
        prev = s
    return " ".join(p for p in parts if p != "")


def _cluster_lines(spans: list[Span], gap: float = 4.0) -> list[str]:
    if not spans:
        return []
    spans = sorted(spans, key=lambda s: (s.y0, s.x0))
    lines: list[list[Span]] = []
    for s in spans:
        if not lines:
            lines.append([s])
            continue
        prev_yc = sum(x.yc for x in lines[-1]) / len(lines[-1])
        if abs(s.yc - prev_yc) <= max(gap, (s.y1 - s.y0) * 0.45):
            lines[-1].append(s)
        else:
            lines.append([s])
    return [_join_line(ln) for ln in lines]


def _join_chars_line(chars: list[Span]) -> str:
    if not chars:
        return ""
    chars = sorted(chars, key=lambda s: s.x0)
    parts: list[str] = []
    prev: Span | None = None
    for c in chars:
        if prev is None:
            parts.append(c.text)
        else:
            gap = c.x0 - prev.x1
            if gap < 1.6 or prev.text.isspace() or c.text.isspace():
                parts[-1] += c.text
            elif gap < 3.2 and (prev.text.isascii() and c.text.isascii()):
                parts[-1] += c.text
            else:
                parts.append(c.text)
        prev = c
    return " ".join(p for p in parts if p.strip() != "" or p.isspace()).replace("  ", " ").strip()


def _text_in(spans: list[Span], x0: float, x1: float, y0: float, y1: float) -> str:
    picked = [s for s in spans if x0 - 0.2 <= s.xc < x1 and y0 < s.yc < y1]
    picked = [s for s in picked if s.text != ""]
    if picked and max(len(s.text) for s in picked) == 1:
        # 글자 단위: 줄 묶은 뒤 글자 이어붙이기
        picked = sorted(picked, key=lambda s: (s.y0, s.x0))
        lines: list[list[Span]] = []
        for s in picked:
            if not lines:
                lines.append([s])
                continue
            prev_yc = sum(x.yc for x in lines[-1]) / len(lines[-1])
            if abs(s.yc - prev_yc) <= max(3.5, (s.y1 - s.y0) * 0.5):
                lines[-1].append(s)
            else:
                lines.append([s])
        return "\n".join(t for t in (_join_chars_line(ln) for ln in lines) if t.strip() != "")
    return "\n".join(t for t in _cluster_lines(picked) if t.strip() != "")


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _is_inherit(s: str) -> bool:
    t = re.sub(r"\s+", "", s)
    return bool(t) and all(c in INHERIT_CHARS for c in t)


def _norm_unit(u: str) -> str:
    return UNIT_NORM.get(u, u)


def _parse_price(raw: str) -> tuple[str, int | None, str]:
    t = raw.strip()
    if "폐지" in t:
        return "폐지", None, "abolished"
    compact = t.replace(" ", "")
    digits = compact.replace(",", "")
    if digits.isdigit():
        val = int(digits)
        return format(val, ","), val, "present"
    return compact, None, "present"


def _parse_labor(raw: str) -> tuple[str, float | None]:
    t = raw.strip()
    m = LABOR_RE.match(t.replace(" ", ""))
    if m:
        s = t.replace(" ", "")
        return s, float(s[:-1])
    return t, None


def _printed(spans: list[Span], x0: float, x1: float, page_h: float) -> int | None:
    def _from(ys: list[Span]) -> int | None:
        ys = sorted(ys, key=lambda s: s.x0)
        texts = [s.text.strip() for s in ys]
        for i in range(len(texts) - 2):
            if texts[i] == "-" and texts[i + 2] == "-" and texts[i + 1].isdigit():
                return int(texts[i + 1])
        joined = " ".join(texts)
        m = re.search(r"-\s*(\d+)\s*-", joined)
        return int(m.group(1)) if m else None

    hit = _from([s for s in spans if s.y0 > page_h * 0.88 and x0 <= s.xc < x1])
    if hit is not None:
        return hit
    # 본문이 짧은 세로쪽: 인쇄쪽이 하단 12%보다 위에 온다
    return _from([s for s in spans if s.y0 > page_h * 0.55 and x0 <= s.xc < x1])


def _header_field(text: str) -> str | None:
    return HEADER_NORM.get(text.strip().replace(" ", ""))


def _header_labels(spans: list[Span], x0: float, x1: float) -> list[tuple[float, dict[str, float]]]:
    """같은 y 띠에 헤더 라벨이 3개 이상 모인 행."""
    cands = [s for s in spans if _header_field(s.text) and x0 <= s.xc < x1]
    cands.sort(key=lambda s: s.yc)
    rows: list[list[Span]] = []
    for s in cands:
        if not rows or abs(s.yc - rows[-1][0].yc) > 8:
            rows.append([s])
        else:
            rows[-1].append(s)
    out = []
    for row in rows:
        yc = sum(s.yc for s in row) / len(row)
        band = [s for s in spans if x0 <= s.xc < x1 and abs(s.yc - yc) <= 8]
        labels: dict[str, float] = {}
        for s in band:
            key = _header_field(s.text)
            if key:
                labels[key] = s.xc
        if "name" not in labels:
            pieces = [s for s in band if s.text.strip() in ("공", "종", "명")]
            if len(pieces) >= 3:
                labels["name"] = sum(s.xc for s in pieces) / len(pieces)
        # 단가 열이 없는 헤더(색인·목록)는 레코드 표가 아니다
        if len(set(labels)) >= 3 and "price" in labels:
            y1 = max(s.y1 for s in row)
            out.append((yc, labels, y1, min(s.y0 for s in row)))
    return out  # type: ignore[return-value]


def _group_headers(spans: list[Span], x0: float, x1: float) -> list[Span]:
    hits = []
    for s in spans:
        if x0 <= s.xc < x1 and "■" in s.text:
            hits.append(s)
    # one per line
    hits.sort(key=lambda s: s.yc)
    uniq: list[Span] = []
    for s in hits:
        if not uniq or abs(s.yc - uniq[-1].yc) > 8:
            uniq.append(s)
    return uniq


def _line_text_at(spans: list[Span], yc: float, x0: float, x1: float, tol: float = 8.0) -> str:
    row = [s for s in spans if x0 <= s.xc < x1 and abs(s.yc - yc) <= tol]
    return _collapse(_join_line(row))


def _banner(spans: list[Span], x0: float, x1: float) -> list[tuple[float, str, str]]:
    """(yc, letter, name) 대분류 배너."""
    found = []
    for s in spans:
        if not (x0 <= s.xc < x1):
            continue
        m = BANNER_RE.search(s.text.replace(" ", ""))
        if not m and "대분류" in s.text:
            # '대분류' + 옆 글자
            rest = _line_text_at(spans, s.yc, x0, x1)
            m = BANNER_RE.search(rest.replace(" ", ""))
            line = rest
        else:
            line = s.text
        if not m:
            continue
        letter = m.group(1)
        # 이름: 같은 상자/바로 아래 줄
        name = ""
        below = [
            t
            for t in spans
            if x0 <= t.xc < x1 and s.y1 - 2 <= t.y0 <= s.y1 + 40 and t is not s
        ]
        below_txt = _collapse(_join_line(sorted(below, key=lambda z: (z.y0, z.x0))))
        below_txt = BANNER_RE.sub("", below_txt.replace("대분류", ""))
        below_txt = re.sub(r"^[A-Z]\s*", "", below_txt).strip()
        if "■" in below_txt or CODE_FIND.search(below_txt) or "공종코드" in below_txt:
            below_txt = ""
        name = re.sub(r"\s+", "", below_txt)
        if len(name) > 24:
            name = ""
        found.append((s.yc, letter, name))
    # unique by letter near same y
    uniq = []
    for item in found:
        if not uniq or abs(item[0] - uniq[-1][0]) > 20 or item[1] != uniq[-1][1]:
            uniq.append(item)
    return uniq


def _codes_and_stars(spans: list[Span], x0: float, x1: float) -> tuple[list[Span], list[Span]]:
    codes, stars = [], []
    half_w = x1 - x0
    for s in spans:
        if not (x0 - 2 <= s.x0 < x1):
            continue
        t = s.text
        for m in STAR_FIND.finditer(t):
            if m.start() > 2:
                continue
            stars.append(Span(s.x0, s.y0, min(s.x1, s.x0 + 78), s.y1, m.group()))
        for m in CODE_FIND.finditer(t):
            if m.start() > 2:
                continue
            # 코드 열(각 단의 왼쪽)만
            if s.x0 > x0 + half_w * 0.28:
                continue
            cw = min(s.x1 - s.x0, 78.0)
            codes.append(Span(s.x0, s.y0, s.x0 + cw, s.y1, m.group()))
    return codes, stars


def _is_real_price_tok(t: str) -> bool:
    """본문 단가/폐지/노무비율. 규격 조각(150, 13, CTC 600)은 제외."""
    s = t.strip().replace(" ", "")
    if not s:
        return False
    if "폐지" in s:
        return True
    if LABOR_RE.match(s):
        return True
    if PRICE_RE.match(s) and ("," in s or len(s) >= 4):
        return True
    return False


def _has_price_on_line(code: Span, spans: list[Span], x0: float, x1: float) -> bool:
    """같은 줄 오른쪽(표 x 의 60% 이후)에 진짜 단가/폐지/노무비율이 있는가."""
    thresh = x0 + (x1 - x0) * 0.60
    for s in spans:
        if s.xc < thresh:
            continue
        if s.xc > x1 + 2:
            continue
        if abs(s.yc - code.yc) > 10:
            continue
        if _is_real_price_tok(s.text):
            return True
    return False


def _half_table_x(
    hlines: list[LineSeg],
    hx0: float,
    hx1: float,
    min_w: float,
) -> tuple[float, float] | None:
    """이 단에서 표 격자 가로선으로 실제 표 x 범위를 잡는다. 없으면 None."""
    need = max(40.0, min_w * 0.5)
    cand: list[LineSeg] = []
    for h in hlines:
        ov = min(h.b, hx1) - max(h.a, hx0)
        if ov >= need:
            cand.append(h)
    if not cand:
        return None
    return max(min(h.a for h in cand), hx0 + 2), min(max(h.b for h in cand), hx1 - 2)


def _is_danga_label(text: str) -> bool:
    """【단가정의】 라벨만. 본문 '단가정의를 참고…' 는 제외."""
    t = text.replace(" ", "").strip()
    if "【단가정의】" in t:
        return True
    if t == "단가정의":
        return True
    return False


def _near_table_h(
    sp: Span,
    hlines: list[LineSeg],
    hx0: float,
    hx1: float,
    min_w: float,
    ytol: float = 50.0,
) -> bool:
    """표 격자(가로선) 근처인가. 칸 단위 짧은 선도 인정. 주석 속 별표는 선이 없다."""
    need = 40.0
    for h in hlines:
        if abs(h.c - sp.yc) > ytol:
            continue
        if min(h.b, hx1) - max(h.a, hx0) >= need:
            return True
    return False


def _price_word_complete(
    price_tok: str,
    words: list[tuple[float, float, float, float, str]],
    price_col: tuple[float, float],
    y0: float,
    y1: float,
) -> bool:
    """짧은 숫자 조각(13, 150, 600)이 규격 낱말에서 온 경우만 거절. 진짜 단가는 통과."""
    if not price_tok or price_tok == "폐지":
        return True
    compact = price_tok.replace(" ", "").replace(",", "")
    if "," in price_tok or len(compact) >= 4:
        return True
    px0, px1 = price_col
    for wx0, wy0, wx1, wy1, t in words:
        yc = (wy0 + wy1) / 2
        if not (y0 - 0.5 < yc < y1 + 0.5):
            continue
        wt = t.strip()
        if not wt:
            continue
        if price_tok not in wt and wt not in price_tok and compact not in wt.replace(",", "").replace(" ", ""):
            continue
        if wx0 >= px0 - 2.5 and wx1 <= px1 + 2.5:
            return True
        return False
    return True


def _vxs_near(vlines: list[LineSeg], y0: float, y1: float, x0: float, x1: float) -> list[float]:
    xs = []
    ymid0, ymid1 = y0, y1
    for v in vlines:
        # overlap in y
        oy0, oy1 = max(v.a, ymid0), min(v.b, ymid1)
        if oy1 - oy0 < 15:
            continue
        if x0 + 8 < v.c < x1 - 8:
            xs.append(v.c)
    return _cluster_xs(xs)


def _wide_h(hlines: list[LineSeg], y0: float, y1: float, min_w: float) -> list[LineSeg]:
    return [h for h in hlines if y0 <= h.c <= y1 and (h.b - h.a) >= min_w]


def _wide_h_table(
    hlines: list[LineSeg],
    y0: float,
    y1: float,
    min_w: float,
    tx0: float,
    tx1: float,
) -> list[LineSeg]:
    need = min(min_w, max(40.0, (tx1 - tx0) * 0.5))
    out = []
    for h in hlines:
        if not (y0 <= h.c <= y1):
            continue
        if (h.b - h.a) < min_w * 0.5:
            continue
        if min(h.b, tx1) - max(h.a, tx0) >= need:
            out.append(h)
    return out


def _build_columns(
    vxs: list[float],
    table_x0: float,
    table_x1: float,
    labels: dict[str, float] | None,
) -> dict[str, tuple[float, float]]:
    extra = []
    if labels and len(labels) >= 2 and len(vxs) < max(5, len(labels) - 1):
        xs = sorted(labels.values())
        extra = [(xs[i] + xs[i + 1]) / 2 for i in range(len(xs) - 1)]
    elif labels and "remark" in labels and "labor" in labels:
        extra.append((labels["labor"] + labels["remark"]) / 2)
    bounds = [table_x0] + vxs + extra + [table_x1]
    bounds = _cluster_xs(bounds, tol=3.0)
    cols: list[tuple[float, float]] = []
    for i in range(len(bounds) - 1):
        if bounds[i + 1] - bounds[i] > 10:
            cols.append((bounds[i], bounds[i + 1]))
    names: list[str | None] = [None] * len(cols)
    if labels:
        for name, xc in labels.items():
            best_i, best_d = None, 1e9
            for i, (a, b) in enumerate(cols):
                if a - 4 <= xc <= b + 4:
                    d = abs((a + b) / 2 - xc)
                    if d < best_d:
                        best_i, best_d = i, d
            if best_i is not None:
                names[best_i] = name
    want_remark = bool(labels and "remark" in labels) or len(cols) >= 7
    pool = DEFAULT7 if want_remark else DEFAULT6
    if names.count(None) == len(names):
        if len(cols) >= 7:
            names = list(DEFAULT7) + [None] * (len(cols) - 7)
            names = names[: len(cols)]
        elif len(cols) >= 6:
            names = list(DEFAULT6) + [None] * (len(cols) - 6)
            names = names[: len(cols)]
        elif len(cols) == 5:
            names = ["code", "name", "spec", "unit", "price"]
        elif len(cols) == 4:
            names = ["name", "spec", "unit", "price"]
    else:
        used = {n for n in names if n}
        for i, n in enumerate(names):
            if n is None:
                for cand in pool:
                    if cand not in used:
                        names[i] = cand
                        used.add(cand)
                        break
    colmap: dict[str, tuple[float, float]] = {}
    for n, (a, b) in zip(names, cols):
        if n:
            colmap[n] = (a, b)
    if "labor" not in colmap and "price" in colmap:
        px0, px1 = colmap["price"]
        right_lim = colmap["remark"][0] if "remark" in colmap else table_x1
        if right_lim - px1 > 20:
            colmap["labor"] = (px1, right_lim)
        elif "labor" not in colmap:
            split = px0 + (px1 - px0) * 0.55
            colmap["price"] = (px0, split)
            colmap["labor"] = (split, max(px1, right_lim))
    if "code" not in colmap and "name" in colmap:
        nx0, nx1 = colmap["name"]
        if nx0 - table_x0 > 30:
            colmap["code"] = (table_x0, nx0)
    return colmap


def _col(colmap: dict[str, tuple[float, float]], name: str, fallback: tuple[float, float]) -> tuple[float, float]:
    return colmap.get(name, fallback)


def _note_items(lines: list[str], pdf_page: int) -> list[dict]:
    items: list[dict] = []
    buf = ""
    for line in lines:
        s = line.strip()
        if not s or s.startswith("【단가정의】") or s.startswith("단가정의"):
            continue
        if _is_note_junk(s):
            continue
        if s.startswith("(표)"):
            if buf:
                items.append({"item": buf.strip(), "pdf_page": pdf_page})
                buf = ""
            items.append({"item": s, "pdf_page": pdf_page, "subtable": True})
            continue
        if s and s[0] in CIRCLED:
            if buf:
                items.append({"item": _trim_note(buf), "pdf_page": pdf_page})
            rest = s[1:].lstrip()
            buf = f"{s[0]} {rest}" if rest else s[0]
        else:
            if buf:
                buf = buf + " " + s
            else:
                buf = s
    if buf and not _is_note_junk(buf):
        items.append({"item": _trim_note(buf), "pdf_page": pdf_page})
    return items


def _trim_note(s: str) -> str:
    t = re.sub(r"\s*-\s*\d+\s*-\s*$", "", s.strip())
    return t.strip()


def _is_note_junk(s: str) -> bool:
    t = s.strip()
    if not t:
        return True
    if t[0] in CIRCLED or t.startswith("(표)") or t.startswith("<"):
        return False
    compact = t.replace(" ", "")
    compact = re.sub(r"-\d+-", "", compact)
    compact = compact.replace("()", "").strip("-").strip()
    if not compact:
        return True
    if re.fullmatch(r"-\d+-", t.replace(" ", "")):
        return True
    if t.startswith("대분류") or compact.startswith("대분류"):
        return True
    if re.match(r"^[A-Z]{2}\d+\*", compact) and len(compact) < 28:
        return True
    if STAR_FIND.fullmatch(compact) or re.fullmatch(r"[A-Z]{2}\d+\*+", compact):
        return True
    # 표 잔여 조각
    if t.endswith("/") and len(t) < 40:
        return True
    if len(compact) < 24 and not re.search(r"[가-힣]{3,}", t):
        return True
    if t.startswith("- ") and re.search(r"(총연장|초과|이하)", t) and "단가" not in t:
        return True
    return False


def _flatten_subtable(table) -> str:
    try:
        rows = table.extract()
    except Exception:
        return ""
    parts = []
    for row in rows:
        cells = [re.sub(r"\s+", " ", (c or "").replace("\n", " ")).strip() for c in row]
        parts.append(" | ".join(cells))
    body = " | ".join(p for p in parts if p.strip("| "))
    return "(표) " + body if body.strip() else ""


def _scan_fields(doc: fitz.Document) -> list[tuple[int, str]]:
    """파일 전체에서 분야 경계를 훑는다. (page, field_name) 오름차순."""
    starts: list[tuple[int, str]] = []
    for i in range(doc.page_count):
        compact = doc[i].get_text("text").replace(" ", "").replace("\n", "")
        m = FIELD_RE.search(compact)
        if m:
            starts.append((i + 1, m.group(1)))
    return starts


def _field_at(starts: list[tuple[int, str]], pno: int) -> str | None:
    name = None
    for sp, fn in starts:
        if sp <= pno:
            name = fn
        else:
            break
    return name


def _row_bands(
    centers: list[float],
    hlines: list[LineSeg],
    table_x0: float,
    table_x1: float,
    table_top: float,
    table_bottom: float,
) -> list[tuple[float, float]]:
    """행 띠. 가로 테두리선이 있으면 우선, 없으면 코드 y 중간점."""
    table_w = table_x1 - table_x0
    min_overlap = max(40.0, table_w * 0.55)
    ys: list[float] = []
    for h in hlines:
        if not (table_top - 1.8 <= h.c <= table_bottom + 1.8):
            continue
        ox0, ox1 = max(h.a, table_x0), min(h.b, table_x1)
        if ox1 - ox0 < min_overlap:
            continue
        ys.append(h.c)
    hys = _cluster_xs(ys, tol=1.6)
    bounds = [table_top]
    for y in hys:
        if y - bounds[-1] > 2.0 and table_bottom - y > 2.0:
            bounds.append(y)
    if table_bottom - bounds[-1] > 1.0:
        bounds.append(table_bottom)
    else:
        bounds[-1] = table_bottom

    bands: list[tuple[float, float]] = []
    for i, cyc in enumerate(centers):
        above = [b for b in bounds if b < cyc - 0.8]
        below = [b for b in bounds if b > cyc + 0.8]
        y0 = max(above) if above else table_top
        y1 = min(below) if below else table_bottom
        prev = centers[i - 1] if i else None
        nxt = centers[i + 1] if i + 1 < len(centers) else None
        if prev is not None and y0 < prev:
            y0 = (prev + cyc) / 2
        if nxt is not None and y1 > nxt:
            y1 = (cyc + nxt) / 2
        if y1 <= y0 + 0.5:
            y0 = (prev + cyc) / 2 if prev is not None else table_top
            y1 = (cyc + nxt) / 2 if nxt is not None else table_bottom
        bands.append((y0, y1))
    return bands


def extract_pdf(
    pdf_path: str | Path,
    half: str,
    pages: tuple[int, int] | None = None,
) -> dict:
    pdf_path = Path(pdf_path)
    sha = _sha256(pdf_path)
    doc = fitz.open(pdf_path)
    field_starts = _scan_fields(doc)
    start, end = (1, doc.page_count) if pages is None else pages
    start = max(1, start)
    end = min(doc.page_count, end)

    records: list[dict] = []
    groups: list[dict] = []
    subheaders: list[dict] = []
    pages_out: list[dict] = []

    last_group: dict | None = None
    last_colmap: dict[str, tuple[float, float]] | None = None
    last_major = ""
    last_major_name = ""
    group_seq = 0

    for pno in range(start, end + 1):
        page = doc[pno - 1]
        layout, halves = _halves(page)
        page_field = _field_at(field_starts, pno)
        spans_all = _page_spans(page)
        chars_all = _page_chars(page)
        words_all = [
            (float(w[0]), float(w[1]), float(w[2]), float(w[3]), w[4])
            for w in page.get_text("words")
            if w[4]
        ]
        hlines, vlines = _page_lines(page)
        try:
            tabs = page.find_tables()
            tables = list(tabs.tables) if tabs else []
        except Exception:
            tables = []
        printed_map: dict[str, int] = {}
        min_w = page.rect.width * (0.35 if layout == "landscape_2up" else 0.45)

        for ph, hx0, hx1 in halves:
            spans = [s for s in spans_all if _in_half(s, hx0, hx1)]
            chars = [s for s in chars_all if _in_half(s, hx0, hx1)]
            printed = _printed(spans_all, hx0, hx1, page.rect.height)
            if printed is not None:
                printed_map[ph] = printed

            headers = _header_labels(spans, hx0, hx1)
            gheads = _group_headers(spans, hx0, hx1)
            banners = _banner(spans, hx0, hx1)
            for _yc, letter, name in banners:
                last_major = letter
                if name:
                    last_major_name = name

            codes_all, stars_all = _codes_and_stars(spans, hx0, hx1)
            # 단가 판정은 반 폭이 아니라 이 표의 실제 x
            tx = _half_table_x(hlines, hx0, hx1, min_w)
            if tx is None and last_colmap:
                tx = (
                    min(a for a, _ in last_colmap.values()),
                    max(b for _, b in last_colmap.values()),
                )
            px0, px1 = tx if tx is not None else (hx0, hx1)
            rec_codes = [c for c in codes_all if _has_price_on_line(c, spans, px0, px1)]
            # 소제목은 코드 열 왼쪽 + 표 격자 근처만(주석 속 ND109.12*** 제외)
            stars = []
            for s in stars_all:
                if s.xc >= hx0 + (hx1 - hx0) * 0.35:
                    continue
                if not _near_table_h(s, hlines, hx0, hx1, min_w):
                    continue
                ln = _line_text_at(spans, s.yc, hx0, hx1, tol=8)
                if ln and ln.lstrip()[:1] in CIRCLED:
                    continue
                stars.append(s)

            # cluster codes+stars into tables
            markers: list[tuple[str, Span]] = [("code", c) for c in rec_codes] + [("star", s) for s in stars]
            markers.sort(key=lambda t: t[1].yc)

            clusters: list[list[tuple[str, Span]]] = []
            cur: list[tuple[str, Span]] = []
            for kind, sp in markers:
                if not cur:
                    cur = [(kind, sp)]
                    continue
                prev = cur[-1][1]
                between_g = any(prev.yc + 4 < g.yc < sp.yc - 4 for g in gheads)
                between_h = any(prev.yc + 4 < h[0] < sp.yc - 12 for h in headers)
                gap = sp.yc - prev.yc
                if between_g or between_h or gap > 95:
                    clusters.append(cur)
                    cur = [(kind, sp)]
                else:
                    cur.append((kind, sp))
            if cur:
                clusters.append(cur)

            # y-ordered events: group headers + clusters + 단가정의 라벨만
            dangas = [s for s in spans if _is_danga_label(s.text)]
            events: list[tuple[float, str, object]] = []
            for g in gheads:
                events.append((g.yc, "group", g))
            for cl in clusters:
                events.append((cl[0][1].yc, "table", cl))
            for d in dangas:
                events.append((d.yc, "notes", d))
            events.sort(key=lambda e: e[0])

            current_group = None  # assigned after first ■ on this half
            half_started = False

            def new_group(header_span: Span) -> dict:
                nonlocal group_seq, last_group, last_major
                group_seq += 1
                raw = _line_text_at(spans, header_span.yc, hx0, hx1, tol=10)
                if raw.startswith("■") and len(raw) > 1 and raw[1] != " ":
                    raw = "■ " + raw[1:]
                if not raw.startswith("■"):
                    raw = "■ " + raw
                header = raw.lstrip("■").strip()
                # major from codes later; banner letter if any
                gid = f"{half}#p{pno}#{group_seq}"
                g = {
                    "group_id": gid,
                    "half": half,
                    "pdf_page": pno,
                    "page_half": ph,
                    "printed_page": printed,
                    "header_raw": raw,
                    "header": header,
                    "major": last_major,
                    "major_name": last_major_name,
                    "field": page_field,
                    "notes": [],
                    "record_count": 0,
                }
                groups.append(g)
                last_group = g
                return g

            def attach_notes_from(y0: float, y1: float, grp: dict | None) -> None:
                if grp is None:
                    return
                # lines between y0 and y1, excluding table codes already handled
                lines_txt: list[str] = []
                band = [s for s in spans if y0 < s.yc < y1]
                # skip if this band is mostly a price table
                raw_lines = _cluster_lines(band, gap=6.0)
                # detect subtables via find_tables
                for t in tables:
                    tb = t.bbox
                    tyc = (tb[1] + tb[3]) / 2
                    if not (y0 < tyc < y1):
                        continue
                    # skip price tables (have 공종코드 or many codes)
                    try:
                        ext0 = (t.extract() or [[]])[0]
                    except Exception:
                        ext0 = []
                    head = " ".join(str(c or "") for c in ext0)
                    if "공종코드" in head or "공종명칭" in head or "공종명" in head:
                        continue
                    full_txt = ""
                    try:
                        full_txt = " ".join(str(c or "") for row in (t.extract() or []) for c in row)
                    except Exception:
                        full_txt = head
                    full_c = full_txt.replace(" ", "")
                    if "대분류" in full_c:
                        # 두 글자 대분류(Q, R) 배너는 정답지가 (표) 로 둔다. 한 글자 배너는 제외.
                        if not re.search(r"대분류\s*[A-Z]\s*[,，]\s*[A-Z]", full_txt):
                            continue
                    ncodes = 0
                    for s in rec_codes:
                        if tb[0] - 5 <= s.xc <= tb[2] + 5 and tb[1] <= s.yc <= tb[3]:
                            ncodes += 1
                    if ncodes >= 1 and t.col_count >= 5:
                        continue
                    # 배너처럼 열이 지나치게 많은 표는, 두 글자 대분류가 아니면 제외
                    if t.col_count >= 12 and "대분류" not in full_c:
                        continue
                    # 사례1 암질 5열 표는 ⑦ 본문에 이미 있고, 사례2(양호 4열)만 (표)
                    row0 = [re.sub(r"\s+", "", str(c or "")) for c in ext0]
                    labels = {"양호", "보통", "불량"}
                    if t.col_count >= 5 and row0 and all(x in labels or x == "" for x in row0) and "보통" in row0:
                        continue
                    flat = _flatten_subtable(t)
                    if flat:
                        lines_txt.append(flat)
                have_sub = any(x.startswith("(표)") for x in lines_txt)
                # text lines
                for ln in raw_lines:
                    s = ln.strip()
                    if not s:
                        continue
                    if "■" in s[:3]:
                        continue
                    if CODE_RE.match(s.split()[0] if s.split() else ""):
                        continue
                    if s in HEADER_MAP or s.replace(" ", "") in HEADER_MAP:
                        continue
                    if any(s.startswith(h) for h in ("공종코드", "공종명칭", "공종명", "규격", "단위", "단가", "노무비율", "비고", "비 고")):
                        continue
                    if _is_note_junk(s):
                        continue
                    # (표) 글줄 중복: 이미 flatten 한 표에 들어 있는 글만 건너뜀
                    if have_sub and s[0] not in CIRCLED and not s.startswith("(표)") and not s.startswith("<"):
                        blob = re.sub(r"\s+", "", "".join(lines_txt))
                        if re.sub(r"\s+", "", s) in blob:
                            continue
                        if re.sub(r"\s+", "", s).startswith("구분"):
                            continue
                        if re.search(r"매끈한마감|보통마감|거친마감", s):
                            continue
                    lines_txt.append(s)
                grp["notes"].extend(_note_items(lines_txt, pno))

            # 쪽 넘김으로 그룹을 끝내지 않음: 첫머리 주석(⑤부터, 【단가정의】 없이 ①)을 이전 그룹에
            first_group_y = min((e[0] for e in events if e[1] == "group"), default=None)
            first_table_y = min((e[0] for e in events if e[1] == "table"), default=None)
            first_notes_y = min((e[0] for e in events if e[1] == "notes"), default=None)
            top_limit = page.rect.height
            for y in (first_group_y, first_table_y):
                if y is not None:
                    top_limit = min(top_limit, y)
            has_early_notes = first_notes_y is not None and first_notes_y < top_limit - 1
            if last_group is not None and not has_early_notes and top_limit > 40:
                attach_notes_from(8.0, top_limit, last_group)

            for i_ev, (ey, etype, payload) in enumerate(events):
                next_y = events[i_ev + 1][0] if i_ev + 1 < len(events) else page.rect.height - 12
                if etype == "group":
                    current_group = new_group(payload)  # type: ignore[arg-type]
                    half_started = True
                elif etype == "table":
                    cl: list[tuple[str, Span]] = payload  # type: ignore[assignment]
                    # group assignment
                    grp = current_group or last_group
                    if current_group is None and last_group is not None:
                        grp = last_group
                    current_group = grp
                    half_started = True

                    rec_sp = [sp for k, sp in cl if k == "code"]
                    star_sp = [sp for k, sp in cl if k == "star"]
                    items_sorted = sorted(cl, key=lambda t: t[1].yc)
                    if not rec_sp and not star_sp:
                        continue
                    y_min = items_sorted[0][1].yc
                    y_max = items_sorted[-1][1].yc

                    # header above this cluster
                    lab = None
                    header_yc = None
                    header_y1 = None
                    for hyc, labels, hy1, hy0 in headers:
                        if hyc < y_min - 2 and y_min - hyc < 90:
                            lab = labels
                            header_yc = hyc
                            header_y1 = hy1
                    # table x from wide H lines
                    y_top_search = (header_yc - 25) if header_yc else (y_min - 40)
                    wide = _wide_h(hlines, y_top_search, y_max + 40, min_w * 0.8)
                    # restrict to this half
                    wide = [h for h in wide if h.a < hx1 - 20 and h.b > hx0 + 20]
                    if wide:
                        table_x0 = min(h.a for h in wide)
                        table_x1 = max(h.b for h in wide)
                        table_x0 = max(table_x0, hx0 + 2)
                        table_x1 = min(table_x1, hx1 - 2)
                    else:
                        table_x0 = hx0 + 40
                        table_x1 = hx1 - 40
                    # 헤더에 단가가 없고 격자 가로선도 없으면 목록/색인
                    if (lab is None or "price" not in lab) and not wide:
                        continue

                    vxs = _vxs_near(vlines, y_min - 50, y_max + 20, table_x0, table_x1)
                    colmap = _build_columns(vxs, table_x0, table_x1, lab)
                    if "code" not in colmap and last_colmap and "code" in last_colmap:
                        old0 = min(a for a, _ in last_colmap.values())
                        old1 = max(b for _, b in last_colmap.values())
                        old_w = old1 - old0 or 1.0
                        new_w = table_x1 - table_x0
                        def _tx(x: float) -> float:
                            return table_x0 + (x - old0) / old_w * new_w
                        colmap = {k: (_tx(a), _tx(b)) for k, (a, b) in last_colmap.items()}
                    if colmap:
                        last_colmap = colmap
                        table_x0 = min(a for a, _ in colmap.values())
                        table_x1 = max(b for _, b in colmap.values())

                    # header bottom / table top
                    if header_yc is not None:
                        hb_lines = _wide_h_table(hlines, header_yc, y_min - 2, min_w * 0.5, table_x0, table_x1)
                        if hb_lines:
                            header_bottom = max(h.c for h in hb_lines)
                        else:
                            header_bottom = header_y1 if header_y1 else header_yc + 12
                        table_top = header_bottom
                    else:
                        top_lines = _wide_h_table(hlines, y_min - 30, y_min - 1, min_w * 0.5, table_x0, table_x1)
                        if not top_lines:
                            # 칸 단위 가로선(전체 폭 미만)도 표 상단
                            for h in hlines:
                                if not (y_min - 30 <= h.c <= y_min - 1):
                                    continue
                                if min(h.b, table_x1) - max(h.a, table_x0) >= 40:
                                    top_lines.append(h)
                        table_top = min((h.c for h in top_lines), default=items_sorted[0][1].y0 - 16)

                    bot_lines = _wide_h_table(
                        hlines, y_max + 2, min(next_y - 2, y_max + 55), min_w * 0.5, table_x0, table_x1
                    )
                    if bot_lines:
                        table_bottom = min(h.c for h in bot_lines)
                    else:
                        table_bottom = items_sorted[-1][1].y1 + 8

                    # 행 띠: 가로 테두리선 우선, 없으면 코드 y 중간점
                    centers = [sp.yc for _, sp in items_sorted]
                    bands = _row_bands(centers, hlines, table_x0, table_x1, table_top, table_bottom)

                    current_sub = None  # (pattern, text)
                    fx = table_x0
                    tx = table_x1
                    code_col = _col(colmap, "code", (fx, fx + 80))
                    name_col = _col(colmap, "name", (code_col[1], code_col[1] + 120))
                    spec_col = _col(colmap, "spec", (name_col[1], name_col[1] + 120))
                    unit_col = _col(colmap, "unit", (spec_col[1], spec_col[1] + 40))
                    price_col = _col(colmap, "price", (unit_col[1], unit_col[1] + 70))
                    labor_col = _col(colmap, "labor", (price_col[1], tx))
                    remark_col = colmap.get("remark")
                    has_remark = remark_col is not None

                    prev_name = ""
                    for (kind, sp), (y0, y1) in zip(items_sorted, bands):
                        if kind == "star":
                            pat = sp.text.strip()
                            # 소제목 텍스트: 코드 열 오른쪽 전체
                            txt = _collapse(_text_in(chars, name_col[0], tx, y0, y1))
                            current_sub = (pat, txt)
                            subheaders.append(
                                {
                                    "half": half,
                                    "pdf_page": pno,
                                    "code_pattern": pat,
                                    "text": txt,
                                    "group_id": grp["group_id"] if grp else None,
                                }
                            )
                            continue

                        code = sp.text.strip()
                        name_raw = _text_in(chars, name_col[0], name_col[1], y0, y1)
                        spec_raw = _text_in(chars, spec_col[0], spec_col[1], y0, y1)
                        unit_raw = _text_in(chars, unit_col[0], unit_col[1], y0, y1)
                        price_raw0 = _text_in(chars, price_col[0], price_col[1], y0, y1)
                        labor_raw0 = _text_in(chars, labor_col[0], labor_col[1], y0, y1)
                        remark_raw = (
                            _text_in(chars, remark_col[0], remark_col[1], y0, y1)
                            if has_remark
                            else ""
                        )

                        # 코드 열이 명칭 첫 글자를 삼킨 경우(맹암거, PHC, L형…) 되돌림
                        gap_txt = _text_in(chars, code_col[0], name_col[0], y0, y1)
                        gap_txt = CODE_FIND.sub("", gap_txt)
                        gap_txt = STAR_FIND.sub("", gap_txt)
                        gap_txt = re.sub(r"\s+", "", gap_txt)
                        if gap_txt and gap_txt not in (code,):
                            if not re.sub(r"\s+", "", name_raw).startswith(gap_txt):
                                name_raw = gap_txt + name_raw

                        # 코드 문자열이 명칭에 섞이면 제거
                        if name_raw.startswith(code):
                            name_raw = name_raw[len(code) :].lstrip()

                        unit = _collapse(unit_raw)
                        # 단위 칸에 여러 토큰이면 가장 짧은/오른쪽
                        if "\n" in unit_raw:
                            unit = _collapse(unit_raw.split("\n")[-1])

                        price_tok = ""
                        for line in (price_raw0 or "").split("\n"):
                            t = line.strip().replace(" ", "")
                            if "폐지" in t or PRICE_RE.match(t):
                                price_tok = t if "폐지" not in t else "폐지"
                                if "폐지" in t:
                                    price_tok = "폐지"
                                break
                        if not price_tok:
                            # 줄 전체에서 재탐색
                            for s in spans:
                                if price_col[0] - 2 <= s.xc < price_col[1] + 2 and y0 < s.yc < y1:
                                    t = s.text.strip().replace(" ", "")
                                    if "폐지" in t:
                                        price_tok = "폐지"
                                        break
                                    if PRICE_RE.match(t):
                                        price_tok = t
                                        break

                        labor_tok = ""
                        for s in spans:
                            if labor_col[0] - 2 <= s.xc < labor_col[1] + 4 and y0 < s.yc < y1:
                                t = s.text.strip().replace(" ", "")
                                if LABOR_RE.match(t) or re.match(r"^[‘'′`]?\d{2}[상하]", t):
                                    labor_tok = s.text.strip()
                                    break
                        if not labor_tok:
                            labor_tok = _collapse(labor_raw0)

                        price_raw, price, status = _parse_price(price_tok or price_raw0)
                        labor_raw, labor_ratio = _parse_labor(labor_tok)
                        abolished_at = None
                        if status == "abolished":
                            labor_ratio = None
                            abolished_at = labor_raw
                            if not abolished_at:
                                abolished_at = labor_tok

                        inherited = _is_inherit(name_raw)
                        name_group = current_sub[0] if current_sub else None
                        if inherited:
                            name = current_sub[1] if current_sub else prev_name
                        else:
                            name = _collapse(name_raw)
                            prev_name = name

                        spec = _collapse(spec_raw) if spec_raw.strip() else spec_raw.strip()

                        # 단가 없는 코드는 본문 아님 (이중 방어)
                        if status == "present" and price is None and "폐지" not in (price_tok or ""):
                            continue
                        if status == "present" and price_tok and not _price_word_complete(
                            price_tok, words_all, price_col, y0, y1
                        ):
                            continue

                        rec = {
                            "key": f"{code}@{half}",
                            "code": code,
                            "half": half,
                            "major": code[0],
                            "field": page_field,
                            "pdf_page": pno,
                            "page_half": ph,
                            "printed_page": printed,
                            "group_id": grp["group_id"] if grp else None,
                            "name_group": name_group if inherited or name_group else None,
                            "bbox": [
                                round(table_x0, 1),
                                round(y0, 1),
                                round(table_x1, 1),
                                round(y1, 1),
                            ],
                            "name_raw": name_raw,
                            "name": name,
                            "name_inherited": inherited,
                            "spec_raw": spec_raw,
                            "spec": spec,
                            "unit_raw": unit_raw,
                            "unit": unit,
                            "unit_norm": _norm_unit(unit),
                            "price_raw": price_raw,
                            "price": price,
                            "labor_raw": labor_raw,
                            "labor_ratio": labor_ratio,
                            "status": status,
                            "source_sha256": sha,
                        }
                        if inherited and current_sub:
                            rec["name_group"] = current_sub[0]
                        if not inherited:
                            rec["name_group"] = None
                        if abolished_at:
                            rec["abolished_at"] = abolished_at
                        if has_remark:
                            rec["remark_raw"] = remark_raw
                            rec["remark"] = _collapse(remark_raw)
                        if grp is not None:
                            if not grp.get("major"):
                                grp["major"] = code[0]
                            grp["record_count"] = grp.get("record_count", 0) + 1
                        records.append(rec)

                elif etype == "notes":
                    grp = current_group or last_group
                    attach_notes_from(ey - 2, next_y, grp)

            # page-top notes without 단가정의 keyword already handled if dangas exist.
            # 쪽 맨 위 이어지는 주석 (번호만)
            if events and events[0][1] != "group":
                pass

        pages_out.append(
            {
                "pdf_page": pno,
                "layout": layout,
                "printed": printed_map,
                "width": int(round(page.rect.width)),
                "height": int(round(page.rect.height)),
                "field": page_field,
            }
        )

    doc.close()
    return {
        "records": records,
        "groups": groups,
        "subheaders": subheaders,
        "pages": pages_out,
        "sha256": sha,
        "half": half,
    }
