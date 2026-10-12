"""그림 영역 잘라 내기 (--figures).

한 단 안에서 레코드 표·■ 줄·주석 줄·배너·쪽 번호 밖에 있는 그림·선·글 묶음을
잡아 200dpi PNG 와 figures_raw.jsonl 을 쓴다. 시험 코드(ref/g11/probe.py)의
영역 정의를 변환기 안으로 옮긴 것이다.
"""
from __future__ import annotations

import collections
import difflib
import json
import re
from pathlib import Path

import numpy as np
import pymupdf

GAP_PT = 12.0
SPLIT_GAP_PT = 40.0
CLIP_PAD_PT = 2.0
PNG_DPI = 200
INK_THR = 253
MIN_REGION_PT = 5.0
Y_SAME_PT = 2.0

HEADER_WORDS = {"공종코드", "공종명칭", "규격", "단위", "단가", "노무비율", "비고"}
RE_PAGENO = re.compile(r"^[-–]?\d+[-–]?$")
RE_BANNER = re.compile(r"^대분류[A-Z가-힣]?([,，][A-Z가-힣])*$")
RE_INDEXCODE = re.compile(r"^n[A-Z]{2}\d{3}\.\d{5}")
RE_LABEL = re.compile(r"^[【\[]단가정의[】\]]$")
RE_SPACE = re.compile(r"[\s\u200b\ufeff\u00a0\ue000-\uf8ff]+")
RE_CODE_ONLY = re.compile(r"^[A-Z]{2}\d{3}\.\d{5}$")
RE_CODE_IN = re.compile(r"[A-Z]{2}\d{3}\.\d{5}")
RE_HANGUL4 = re.compile(r"[가-힣]{4}")

RAW_KEYS = (
    "fig_id",
    "half",
    "group_id",
    "group_header",
    "k",
    "pdf_page",
    "printed_page",
    "page_half",
    "bbox",
    "lines",
    "n_images",
    "n_drawings",
    "file",
    "split_parts",
)


def compact(s: str | None) -> str:
    return RE_SPACE.sub("", s or "")


def is_figure_title_text(c: str) -> bool:
    """그림 제목 꼴: [표준도] · [그림 · <사례 · <… 상세도> 등."""
    return bool(c) and c[0] in "[<("


def record_index(records: list[dict] | None) -> tuple[dict[str, list[tuple[str, str]]], set[str], set[str]]:
    codes: dict[str, list[tuple[str, str]]] = {}
    prices: set[str] = set()
    labors: set[str] = set()
    for rc in records or []:
        code = compact(rc.get("code") or "")
        pr = compact(rc.get("price_raw") or "")
        lb = compact(rc.get("labor_raw") or "")
        if code:
            codes.setdefault(code, []).append((pr, lb))
        if pr:
            prices.add(pr)
        if lb:
            labors.add(lb)
    return codes, prices, labors


def note_compacts(groups: list[dict] | None) -> list[str]:
    out: list[str] = []
    for g in groups or []:
        for n in g.get("notes") or []:
            c = compact(n.get("item") or "")
            if c:
                out.append(c)
    return out


def line_is_header(c: str) -> bool:
    if not c:
        return False
    if c.startswith("■"):
        return True
    if RE_LABEL.match(c):
        return True
    if RE_BANNER.match(c):
        return True
    if RE_PAGENO.match(c):
        return True
    return False


def line_is_note_sentence(c: str, notes: list[str]) -> bool:
    if not c or is_figure_title_text(c):
        return False
    if len(c) < 10 or not RE_HANGUL4.search(c):
        return False
    return any(c in n for n in notes)


def line_is_amount_cell(c: str, prices: set[str], labors: set[str]) -> bool:
    """칸 전체가 단가·노무비율이거나, 단가와 노무비율이 한 덩어리로 붙은 줄."""
    if not c:
        return False
    if c in prices or c in labors:
        return True
    for pr in prices:
        if pr and c.startswith(pr):
            rest = c[len(pr):]
            if not rest or rest in labors:
                return True
    for lb in labors:
        if lb and c.startswith(lb):
            rest = c[len(lb):]
            if not rest or rest in prices:
                return True
    return False


def line_is_table_cell(
    c: str,
    peer_compacts: set[str],
    codes: dict[str, list[tuple[str, str]]],
    prices: set[str],
    labors: set[str],
) -> bool:
    """C-1: 단가·노무비율 칸, 또는 같은 줄/같은 높이(±2pt)에 단가가 있는 공종코드.

    공종코드만 있는 줄은 도해 라벨로 남긴다.
    """
    if not c:
        return False
    if c in HEADER_WORDS:
        return True
    if line_is_amount_cell(c, prices, labors):
        return True
    if RE_CODE_ONLY.match(c):
        for pr, lb in codes.get(c) or []:
            prs = {pr} if pr else set()
            lbs = {lb} if lb else set()
            if any(line_is_amount_cell(p, prs, lbs) for p in peer_compacts):
                return True
        return False
    m = RE_CODE_IN.search(c)
    if m:
        rest = c[m.end():] or c[: m.start()]
        if line_is_amount_cell(rest, prices, labors) or line_is_amount_cell(c[: m.start()] + c[m.end():], prices, labors):
            return True
    return False


def c_exclude_reason(
    compact_text: str,
    peer_compacts: set[str],
    codes: dict[str, list[tuple[str, str]]],
    prices: set[str],
    labors: set[str],
    notes: list[str],
) -> str | None:
    """C-1 table / C-2 note / C-3 header. None 이면 그림 후보."""
    c = compact_text
    if line_is_header(c):
        return "header"
    if line_is_note_sentence(c, notes):
        return "note"
    if line_is_table_cell(c, peer_compacts, codes, prices, labors):
        return "table"
    return None


def region_is_junk(box, items) -> bool:
    """D: 5pt 미만, 빈 영역, 그림 제목 꼴이 아닌 글 조각."""
    if (box[2] - box[0]) < MIN_REGION_PT or (box[3] - box[1]) < MIN_REGION_PT:
        return True
    n_img = sum(1 for it in items if it.get("kind") == "img")
    n_draw = sum(1 for it in items if it.get("kind") == "draw")
    texts = [it for it in items if it.get("kind") in ("text", "diagram_label")]
    if n_img == 0 and n_draw == 0:
        if not texts:
            return True
        if not all(is_figure_title_text(compact(it.get("text") or "")) for it in texts):
            return True
    return False


def _merge_y_bands(bands: list[list[float]]) -> list[list[float]]:
    if not bands:
        return []
    bands = sorted(bands)
    out = [list(bands[0])]
    for a, b in bands[1:]:
        if a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def split_region_excluded(r: dict, y_tol: float = Y_SAME_PT) -> list[dict]:
    """C-5: 뺀 줄 띠로 영역을 나누고, 남은 항목으로 상자를 다시 정한다."""
    box = r["box"]
    bands: list[list[float]] = []
    for eb in r.get("excluded_boxes") or []:
        if eb[2] < box[0] or eb[0] > box[2]:
            continue
        if eb[3] < box[1] or eb[1] > box[3]:
            continue
        bands.append([eb[1] - y_tol, eb[3] + y_tol])
    bands = _merge_y_bands(bands)
    if not bands:
        return [r]
    slices: list[list[float]] = []
    y = box[1]
    for a, b in bands:
        a = max(a, box[1])
        b = min(b, box[3])
        if a - y >= 0.01:
            slices.append([box[0], y, box[2], a])
        y = max(y, b)
    if box[3] - y >= 0.01:
        slices.append([box[0], y, box[2], box[3]])
    out: list[dict] = []
    for sl in slices:
        items = []
        for it in r["items"]:
            ib = it["box"]
            ix0, iy0 = max(ib[0], sl[0]), max(ib[1], sl[1])
            ix1, iy1 = min(ib[2], sl[2]), min(ib[3], sl[3])
            if ix1 - ix0 > 0.01 and iy1 - iy0 > 0.01:
                nit = dict(it)
                nit["box"] = [ix0, iy0, ix1, iy1]
                items.append(nit)
        if not items:
            continue
        nb = union([it["box"] for it in items])
        nb = [max(nb[0], sl[0]), max(nb[1], sl[1]), min(nb[2], sl[2]), min(nb[3], sl[3])]
        rr = dict(r)
        rr["box"] = nb
        rr["items"] = items
        out.append(rr)
    return out


def inter(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return (w, h)


def center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def inside_center(b, box, m=0):
    cx, cy = center(b)
    return box[0] - m <= cx <= box[2] + m and box[1] - m <= cy <= box[3] + m


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def rnd_box(b):
    return [round(float(v), 1) for v in b]


def col_rect(page_w, page_h, layout, half):
    if layout == "landscape_2up" and half in ("L", "R"):
        return [0, 0, page_w / 2, page_h] if half == "L" else [page_w / 2, 0, page_w, page_h]
    return [0, 0, page_w, page_h]


def fig_id_of(group_id: str | None, k: int) -> str:
    gid = group_id if group_id is not None else "unassigned"
    return f"{gid}#f{k}"


def fig_filename(fig_id: str) -> str:
    return fig_id.replace("#", "_") + ".png"


def group_code_set(group_id: str | None, records: list[dict]) -> frozenset[str]:
    if not group_id:
        return frozenset()
    return frozenset(r["code"] for r in records if r.get("group_id") == group_id and r.get("code"))


def match_groups_by_codes(
    src: list[tuple[str, frozenset[str]]],
    dst: list[tuple[str, frozenset[str]]],
    min_jaccard: float = 0.0,
) -> dict[str, str | None]:
    """src group_id → dst group_id. 공종코드 묶음 자카드가 가장 큰 1:1 짝.

    min_jaccard=0 이면 교집합이 있기만 하면 잇고, 없으면 None.
    """
    pairs: list[tuple[float, str, str]] = []
    for sid, ss in src:
        if not ss:
            continue
        for did, ds in dst:
            if not ds:
                continue
            inter_n = len(ss & ds)
            if inter_n == 0:
                continue
            uni = len(ss | ds)
            score = inter_n / uni if uni else 0.0
            if score >= min_jaccard:
                pairs.append((score, sid, did))
    pairs.sort(key=lambda x: (-x[0], x[1], x[2]))
    used_s: set[str] = set()
    used_d: set[str] = set()
    out: dict[str, str | None] = {sid: None for sid, _ in src}
    for _score, sid, did in pairs:
        if sid in used_s or did in used_d:
            continue
        out[sid] = did
        used_s.add(sid)
        used_d.add(did)
    return out


def _build_S(g: dict):
    s = ""
    items = []
    subs = []
    for i, n in enumerate(g.get("notes") or []):
        if n.get("subtable"):
            subs.append((len(s), i, n))
            continue
        c = compact(n.get("item_raw") or n.get("item") or "")
        items.append((len(s), len(s) + len(c), i))
        s += c
    return s, items, subs


def analyze(doc, pages: list[dict], records: list[dict], groups: list[dict], gap: float = GAP_PT, hwp_records: list[dict] | None = None):
    """쪽·단을 훑어 남은 그림·선·글 묶음의 재료를 모은다."""
    pinfo = {p["pdf_page"]: p for p in pages if p.get("pdf_page") is not None}
    content_pages = set(r["pdf_page"] for r in records if r.get("pdf_page") is not None) | set(
        g["pdf_page"] for g in groups if g.get("pdf_page") is not None
    ) | set(
        n["pdf_page"] for g in groups for n in (g.get("notes") or []) if n.get("pdf_page") is not None
    )
    rec_by_col: dict[tuple, list] = collections.defaultdict(list)
    for r in records:
        if r.get("pdf_page") is None:
            continue
        rec_by_col[(r["pdf_page"], r["page_half"])].append(r)
    grp_by_col: dict[tuple, list] = collections.defaultdict(list)
    for g in groups:
        if g.get("pdf_page") is None:
            continue
        grp_by_col[(g["pdf_page"], g["page_half"])].append(g)
    gid2g = {g["group_id"]: g for g in groups}
    cols = []
    for pno in sorted(content_pages):
        p = pinfo.get(pno)
        if p is None:
            continue
        halves = ["L", "R"] if p["layout"] == "landscape_2up" else ["C"]
        for h in halves:
            cols.append((pno, h))

    explained_total = collections.Counter()
    cur_group = None
    S = ""
    ptr = 0
    S_items: list = []
    sub_state = None
    stats = collections.Counter()
    col_info: dict = {}
    recs_for_c = hwp_records if hwp_records else records
    codes_idx, prices_idx, labors_idx = record_index(recs_for_c)
    notes_idx = note_compacts(groups)

    for (pno, half) in cols:
        page = doc[pno - 1]
        p = pinfo[pno]
        W, H = page.rect.width, page.rect.height
        cr = col_rect(W, H, p["layout"], half)
        lines = []
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                t = "".join(s["text"] for s in l["spans"])
                bb = list(l["bbox"])
                if p["layout"] == "landscape_2up":
                    cx = (bb[0] + bb[2]) / 2
                    if (half == "L") != (cx < W / 2):
                        continue
                lines.append({"box": bb, "text": t, "c": compact(t)})
        lines.sort(key=lambda x: (round(x["box"][1] / 2), x["box"][0]))

        recs = rec_by_col.get((pno, half), [])
        tabs: dict = collections.defaultdict(list)
        for r in recs:
            tabs[(r["group_id"], r["table_seq"])].append(r["bbox"])
        hdr_lines = [l for l in lines if l["c"] in HEADER_WORDS]
        table_boxes = []
        table_hy = []
        hdr_ids = set()
        for _k, bl in tabs.items():
            bx = union(bl)
            cand = [l for l in hdr_lines if l["box"][3] <= bx[1] + 3 and l["box"][1] >= bx[1] - 70]
            if cand:
                hy = max(l["box"][1] for l in cand)
                row = [l for l in hdr_lines if abs(l["box"][1] - hy) < 4]
                for l in row:
                    hdr_ids.add(id(l))
                top = min(l["box"][1] for l in row) - 16
                table_hy.append(min(l["box"][1] for l in row))
            else:
                top = bx[1] - 4
            table_boxes.append([bx[0] - 4, top, bx[2] + 4, bx[3] + 4])

        title_lines = [l for l in lines if l["c"].startswith("■")]
        col_groups = grp_by_col.get((pno, half), [])
        title_map = {}
        used = set()
        for l in title_lines:
            gm = None
            for g in col_groups:
                if g["group_id"] in used:
                    continue
                hc = compact(g.get("header_raw") or ("■" + (g.get("header") or "")))
                if hc == l["c"] or l["c"].startswith(hc[:12]) or (len(l["c"]) >= 3 and hc.startswith(l["c"])):
                    gm = g
                    break
            if gm is None:
                for g in col_groups:
                    if g["group_id"] not in used:
                        gm = g
                        stats["title_order_fallback"] += 1
                        break
            if gm is not None:
                used.add(gm["group_id"])
                title_map[id(l)] = gm
            else:
                stats["title_unmatched"] += 1

        banner_bands = []
        for l in lines:
            if RE_BANNER.match(l["c"]):
                banner_bands.append([cr[0], l["box"][1] - 10, cr[2], l["box"][3] + 10])
        sub_boxes = []
        cls = []
        title_pos = []
        col_start_group = cur_group
        cur_title_box = None
        cur_title_c = ""
        index_band = None
        for l in lines:
            c = l["c"]
            bb = l["box"]
            kind = None
            if c == "":
                kind = "blank"
            elif RE_PAGENO.match(c):
                kind = "pageno"
            elif id(l) in hdr_ids or any(inside_center(bb, tb) for tb in table_boxes):
                kind = "table"
            elif any(inside_center(bb, bb2) for bb2 in banner_bands):
                kind = "banner"
            elif (
                cur_title_box is not None
                and bb[0] >= cur_title_box[2] - 1
                and abs((bb[1] + bb[3]) / 2 - (cur_title_box[1] + cur_title_box[3]) / 2) < 4
                and c in cur_title_c
            ):
                kind = "title"
            elif id(l) in title_map:
                kind = "title"
                if sub_state and sub_state["active"] is not None and sub_state["active"]["boxes"]:
                    a = sub_state["active"]
                    sub_boxes.append(union(a["boxes"]))
                    sub_state["done"].add(a["idx"])
                    sub_state["active"] = None
                cur_group = title_map[id(l)]
                S, S_items, subs = _build_S(cur_group)
                ptr = 0
                sub_state = {"subs": subs, "active": None, "done": set(), "matched": 0}
                title_pos.append((bb[1], cur_group["group_id"], l["text"]))
                l["group"] = cur_group
                cur_title_box = bb
                cur_title_c = compact(cur_group.get("header_raw") or "")
            elif RE_INDEXCODE.match(c) or (index_band is not None and abs((bb[1] + bb[3]) / 2 - index_band) < 5):
                kind = "index"
                if RE_INDEXCODE.match(c):
                    index_band = (bb[1] + bb[3]) / 2
            elif c.startswith("■"):
                kind = "title"
                stats["title_line_nogroup"] += 1
            elif RE_LABEL.match(c):
                kind = "label"
            else:
                if cur_group is not None:
                    st = sub_state
                    if st and st["subs"]:
                        for (pos, idx, n) in st["subs"]:
                            if idx in st["done"]:
                                continue
                            if ptr >= pos and st["active"] is None:
                                cells = re.split(r"\s*[|/]\s*", (n.get("item_raw") or "").replace("(표)", "", 1))
                                T = "".join(compact(x) for x in cells)
                                st["active"] = {"idx": idx, "T": T, "matched": 0, "boxes": []}
                    act = st["active"] if st else None
                    matched = False
                    if act is not None and c and c in act["T"]:
                        act["matched"] += len(c)
                        act["boxes"].append(bb)
                        kind = "subtable"
                        matched = True
                    if not matched:
                        if S.startswith(c, ptr) and c:
                            ptr += len(c)
                            matched = True
                        elif len(c) >= 6:
                            j = S.find(c, ptr, ptr + 400)
                            if j >= 0:
                                ptr = j + len(c)
                                matched = True
                        if not matched and len(c) >= 4 and ptr < len(S):
                            win = S[ptr : ptr + len(c) + 14]
                            sm = difflib.SequenceMatcher(None, c, win, autojunk=False)
                            blks = [b for b in sm.get_matching_blocks() if b.size > 0]
                            if blks and blks[0].b <= 6 and sum(b.size for b in blks) >= 0.75 * len(c):
                                ptr += blks[-1].b + blks[-1].size
                                matched = True
                                stats["fuzzy_note_line"] += 1
                        if matched:
                            kind = "note"
                    if kind is None:
                        kind = "remain"
                else:
                    kind = "remain"
            if sub_state and sub_state["active"] is not None:
                a = sub_state["active"]
                if kind != "subtable" and a["matched"] >= 0.7 * len(a["T"]):
                    sub_boxes.append(union(a["boxes"]))
                    sub_state["done"].add(a["idx"])
                    sub_state["active"] = None
            cls.append((l, kind))
        if sub_state and sub_state["active"] is not None and sub_state["active"]["boxes"]:
            a = sub_state["active"]
            sub_boxes.append(union(a["boxes"]))
            sub_state["done"].add(a["idx"])
            sub_state["active"] = None
        sub_boxes = [[b[0] - 10, b[1] - 10, b[2] + 10, b[3] + 10] for b in sub_boxes]
        for i, (l, k) in enumerate(cls):
            if k == "remain" and any(0 < hy - l["box"][3] <= 28 for hy in table_hy):
                cls[i] = (l, "subcap")

        def _yc_box(b):
            return (b[1] + b[3]) / 2

        all_line_yc = [(_yc_box(l["box"]), l["c"]) for l, _k in cls]
        c_boxes: list = []
        for i, (l, k) in enumerate(cls):
            yc = _yc_box(l["box"])
            peers = {c for yc2, c in all_line_yc if abs(yc2 - yc) <= Y_SAME_PT}
            reason = c_exclude_reason(l["c"], peers, codes_idx, prices_idx, labors_idx, notes_idx)
            if reason:
                eb = list(l["box"])
                if l["c"] in HEADER_WORDS:
                    eb[1] -= 24.0
                c_boxes.append(eb)
            if k != "remain":
                continue
            if reason == "table":
                cls[i] = (l, "table")
                lb = l["box"]
                pad_top = 24.0 if l["c"] in HEADER_WORDS else 4.0
                table_boxes.append([lb[0] - 4, lb[1] - pad_top, lb[2] + 4, lb[3] + 4])
                if l["c"] in HEADER_WORDS:
                    c_boxes.append([lb[0], lb[1] - pad_top, lb[2], lb[3]])
            elif reason == "note":
                cls[i] = (l, "note")
            elif reason == "header":
                if l["c"].startswith("■"):
                    cls[i] = (l, "title")
                elif RE_LABEL.match(l["c"]):
                    cls[i] = (l, "label")
                elif RE_BANNER.match(l["c"]):
                    cls[i] = (l, "banner")
                else:
                    cls[i] = (l, "pageno")
            elif RE_CODE_ONLY.match(l["c"]):
                cls[i] = (l, "diagram_label")
        table_yc = [_yc_box(l["box"]) for l, k in cls if k == "table"]
        for i, (l, k) in enumerate(cls):
            if k != "remain":
                continue
            yc = _yc_box(l["box"])
            if any(abs(yc - tyc) <= Y_SAME_PT for tyc in table_yc):
                cls[i] = (l, "table")
                c_boxes.append(l["box"])
                lb = l["box"]
                table_boxes.append([lb[0] - 4, lb[1] - 4, lb[2] + 4, lb[3] + 4])
        excluded_boxes = c_boxes

        for l, k in cls:
            explained_total[k] += 1

        draws = []
        for d in page.get_drawings():
            r = d["rect"]
            bb = [r.x0, r.y0, r.x1, r.y1]
            if d.get("type") == "f" and d.get("fill") and all(v > 0.97 for v in d["fill"]) and not d.get("color"):
                continue
            if not inside_center(bb, cr):
                continue
            draws.append(bb)
        n_draw_all = len(draws)

        def grow_sub(sb):
            sb = list(sb)
            ch = True
            while ch:
                ch = False
                for bb in draws:
                    if any(inside_center(bb, tb, 2) for tb in table_boxes):
                        continue
                    if bb[0] <= sb[2] + 14 and bb[2] >= sb[0] - 14 and bb[1] <= sb[3] + 4 and bb[3] >= sb[1] - 4:
                        nb = union([sb, bb])
                        if nb != sb and (nb[3] - nb[1]) < (sb[3] - sb[1]) + 40 and (nb[2] - nb[0]) < (sb[2] - sb[0]) + 80:
                            sb = nb
                            ch = True
            return sb

        sub_boxes = [grow_sub(sb) for sb in sub_boxes]
        expl_line_boxes = [
            l["box"] for l, k in cls if k in ("title", "label", "note", "pageno", "banner", "blank", "index", "subcap")
        ]

        def _in_excl_band(bb):
            yc = _yc_box(bb)
            for eb in excluded_boxes:
                if eb[1] - Y_SAME_PT <= yc <= eb[3] + Y_SAME_PT:
                    return True
            return False

        d_remain = []
        for bb in draws:
            ok = False
            if any(inside_center(bb, tb, 2) for tb in table_boxes):
                ok = True
            elif any(inside_center(bb, el, 2) for el in expl_line_boxes):
                ok = True
            elif any(inside_center(bb, sb, 2) for sb in sub_boxes):
                ok = True
            elif any(inside_center(bb, bd, 0) for bd in banner_bands):
                ok = True
            elif _in_excl_band(bb):
                ok = True
            if not ok:
                d_remain.append(bb)

        ims = []
        for im in page.get_image_info():
            bb = list(im["bbox"])
            if not inside_center(bb, cr):
                continue
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            if w <= 5 and h <= 5 and (bb[0] < 8 or bb[1] < 8 or bb[2] > W - 8 or bb[3] > H - 8):
                continue
            ims.append(bb)
        explained_boxes = [l["box"] for l, k in cls if k not in ("remain", "diagram_label")] + table_boxes + sub_boxes + banner_bands
        i_remain = []
        if ims:
            pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w).copy()
            sx, sy = pix.w / W, pix.h / H
            for eb in explained_boxes:
                x0, y0 = max(0, int(eb[0] * sx) - 1), max(0, int(eb[1] * sy) - 1)
                x1, y1 = min(pix.w, int(eb[2] * sx) + 2), min(pix.h, int(eb[3] * sy) + 2)
                arr[y0:y1, x0:x1] = 255
            for bb in ims:
                cb = [max(bb[0], cr[0]), max(bb[1], cr[1]), min(bb[2], cr[2]), min(bb[3], cr[3])]
                if cb[2] <= cb[0] or cb[3] <= cb[1]:
                    continue
                x0, y0, x1, y1 = int(cb[0] * sx), int(cb[1] * sy), int(cb[2] * sx) + 1, int(cb[3] * sy) + 1
                sub = arr[y0:y1, x0:x1] < INK_THR
                if not sub.any():
                    stats["image_blank_dropped"] += 1
                    continue
                ys, xs = np.where(sub)
                ib = [
                    float((x0 + xs.min()) / sx),
                    float((y0 + ys.min()) / sy),
                    float((x0 + xs.max() + 1) / sx),
                    float((y0 + ys.max() + 1) / sy),
                ]
                i_remain.append({"box": ib, "orig": bb})

        items = []
        for l, k in cls:
            if k == "remain":
                items.append({"kind": "text", "box": l["box"], "text": l["text"]})
            elif k == "diagram_label":
                items.append({"kind": "diagram_label", "box": l["box"], "text": l["text"]})
        for bb in d_remain:
            items.append({"kind": "draw", "box": bb})
        for ib in i_remain:
            items.append({"kind": "img", "box": ib["box"]})
        items.sort(key=lambda x: (x["box"][1], x["box"][0]))
        has_content = bool(table_boxes) or any(k in ("title", "note", "label") for l, k in cls)
        excluded = not has_content
        col_info[(pno, half)] = {
            "excluded": excluded,
            "W": W,
            "H": H,
            "cr": cr,
            "cls": cls,
            "table_boxes": table_boxes,
            "sub_boxes": sub_boxes,
            "banner_bands": banner_bands,
            "items": items,
            "excluded_boxes": excluded_boxes,
            "n_draw_all": n_draw_all,
            "n_draw_remain": len(d_remain),
            "n_img": len(i_remain),
            "title_pos": title_pos,
            "col_start_group": col_start_group,
        }

    return {
        "col_info": col_info,
        "cols": cols,
        "gid2g": gid2g,
        "pinfo": pinfo,
        "groups": groups,
        "records": records,
        "doc": doc,
        "stats": stats,
        "explained_total": explained_total,
        "gap": gap,
    }


def cluster(items, gap):
    regs = []
    for it in items:
        if regs and it["box"][1] - regs[-1]["box"][3] <= gap:
            r = regs[-1]
            r["box"] = union([r["box"], it["box"]])
            r["items"].append(it)
        else:
            regs.append({"box": list(it["box"]), "items": [it]})
    return regs


def make_regions(R, gap):
    out = []
    gid2g = R["gid2g"]
    for (pno, half), ci in R["col_info"].items():
        if ci["excluded"]:
            continue
        items = sorted(ci["items"], key=lambda x: (x["box"][1], x["box"][0]))
        for r in cluster(items, gap):
            g = ci["col_start_group"]
            for (y0, gid, _tx) in ci["title_pos"]:
                if y0 < r["box"][1]:
                    g = gid2g[gid]
            r["group"] = g
            r["page"] = pno
            r["col"] = half
            r["cr"] = ci["cr"]
            r["H"] = ci["H"]
            r["split_parts"] = 1
            r["excluded_boxes"] = ci.get("excluded_boxes") or []
            out.append(r)
    return out


def merge_split_regions(regions):
    """같은 단·같은 그룹에서 40pt 안에 붙은 영역을 한 영역으로 합친다. 단·쪽을 넘기지 않는다."""
    by_col: dict[tuple, list] = collections.defaultdict(list)
    for r in regions:
        by_col[(r["page"], r["col"])].append(r)
    out = []
    for key in sorted(by_col, key=lambda x: (x[0], {"L": 0, "R": 1, "C": 2}.get(x[1], 9))):
        lst = sorted(by_col[key], key=lambda z: z["box"][1])
        merged = []
        for r in lst:
            if merged:
                a = merged[-1]
                gid_a = a["group"]["group_id"] if a.get("group") else None
                gid_b = r["group"]["group_id"] if r.get("group") else None
                if gid_a == gid_b and r["box"][1] - a["box"][3] <= SPLIT_GAP_PT:
                    a["box"] = union([a["box"], r["box"]])
                    a["items"].extend(r["items"])
                    a["split_parts"] = a.get("split_parts", 1) + r.get("split_parts", 1)
                    continue
            rr = dict(r)
            rr["split_parts"] = r.get("split_parts", 1)
            merged.append(rr)
        out.extend(merged)
    return out


def cleanup_regions(regions: list[dict]) -> list[dict]:
    """합친 뒤 C-5 로 뺀 줄을 가르고 D 로 작은·글 조각 영역을 버린다."""
    out: list[dict] = []
    for r in regions:
        for piece in split_region_excluded(r):
            if region_is_junk(piece["box"], piece["items"]):
                continue
            out.append(piece)
    return out


def _half_order(h: str) -> int:
    return {"L": 0, "R": 1, "C": 2}.get(h, 9)


def _region_lines(r) -> list[str]:
    return [it["text"].strip() for it in r["items"] if it["kind"] == "text" and (it.get("text") or "").strip()]


def _printed_page(pinfo, pno, half, hwp_mode: bool):
    if hwp_mode:
        return None
    p = pinfo.get(pno) or {}
    printed = p.get("printed") or {}
    return printed.get(half)


def extract_figures(
    pdf_path: str | Path,
    result: dict,
    out_dir: str | Path,
    *,
    hwp_result: dict | None = None,
) -> list[dict]:
    """그림 영역을 잡아 PNG 와 figures_raw.jsonl 을 쓰고 날것 목록을 돌려준다."""
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    half = result.get("half") or (hwp_result or {}).get("half") or ""
    pages = result.get("pages") or []
    records = result.get("records") or []
    groups = result.get("groups") or []
    doc = pymupdf.open(pdf_path)
    try:
        R = analyze(doc, pages, records, groups, GAP_PT, hwp_records=(hwp_result or {}).get("records") if hwp_result else None)
        regions = cleanup_regions(merge_split_regions(make_regions(R, GAP_PT)))
        regions.sort(key=lambda r: (r["page"], _half_order(r["col"]), r["box"][1], r["box"][0]))

        hwp_mode = hwp_result is not None
        unmatched: list[str] = []
        if hwp_mode:
            pdf_sets = [(g["group_id"], group_code_set(g["group_id"], records)) for g in groups]
            hwp_groups = hwp_result.get("groups") or []
            hwp_recs = hwp_result.get("records") or []
            hwp_sets = [(g["group_id"], group_code_set(g["group_id"], hwp_recs)) for g in hwp_groups]
            mapping = match_groups_by_codes(pdf_sets, hwp_sets, min_jaccard=0.0)
            hwp_gmap = {g["group_id"]: g for g in hwp_groups}
            for r in regions:
                pg = r.get("group")
                pid = pg["group_id"] if pg else None
                hid = mapping.get(pid) if pid else None
                if hid:
                    r["group"] = hwp_gmap[hid]
                else:
                    if pid:
                        unmatched.append(pid)
                    r["group"] = None
            half = hwp_result.get("half") or half

        k_count: dict[str, int] = collections.Counter()
        own_dir = out_dir / "figures" / "own"
        own_dir.mkdir(parents=True, exist_ok=True)
        for old in own_dir.glob("*.png"):
            old.unlink()
        rows: list[dict] = []
        pinfo = R["pinfo"]
        for r in regions:
            g = r.get("group")
            gid = g["group_id"] if g else None
            key = gid if gid is not None else "unassigned"
            k_count[key] += 1
            k = k_count[key]
            fid = fig_id_of(gid, k)
            fname = fig_filename(fid)
            rel = f"figures/own/{fname}"
            cr = r["cr"]
            b = r["box"]
            clip = pymupdf.Rect(
                max(b[0] - CLIP_PAD_PT, cr[0]),
                max(b[1] - CLIP_PAD_PT, cr[1]),
                min(b[2] + CLIP_PAD_PT, cr[2]),
                min(b[3] + CLIP_PAD_PT, cr[3]),
            )
            if clip.x1 <= clip.x0 or clip.y1 <= clip.y0:
                clip = pymupdf.Rect(b[0], b[1], max(b[2], b[0] + 1), max(b[3], b[1] + 1))
            page = doc[r["page"] - 1]
            pix = page.get_pixmap(clip=clip, dpi=PNG_DPI)
            pix.save(str(own_dir / fname))
            n_img = sum(1 for it in r["items"] if it["kind"] == "img")
            n_draw = sum(1 for it in r["items"] if it["kind"] == "draw")
            row = {
                "fig_id": fid,
                "half": half,
                "group_id": gid,
                "group_header": (g.get("header") if g else None),
                "k": k,
                "pdf_page": r["page"],
                "printed_page": _printed_page(pinfo, r["page"], r["col"], hwp_mode),
                "page_half": r["col"],
                "bbox": rnd_box(r["box"]),
                "lines": _region_lines(r),
                "n_images": n_img,
                "n_drawings": n_draw,
                "file": rel,
                "split_parts": int(r.get("split_parts") or 1),
            }
            rows.append(row)
    finally:
        doc.close()

    raw_path = out_dir / "figures_raw.jsonl"
    with raw_path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps({k: row[k] for k in RAW_KEYS}, ensure_ascii=False) + "\n")
    if unmatched:
        seen = []
        for x in unmatched:
            if x not in seen:
                seen.append(x)
        print(f"경고: 2026H1 그림 공종 연결 실패 {len(seen)}건", flush=True)
    return rows
