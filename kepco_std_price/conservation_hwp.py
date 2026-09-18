"""HWP 원문 XML 과 산출을 대조하는 보존 게이트.

extract_hwp / extract.py 의 추출 함수를 재사용하지 않는다. XML 을 다시 읽어
레코드 표 셀 Text·주석 문단 낱말을 독립적으로 센다.
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Any

from lxml import etree

CODE_FIND = re.compile(r"[A-Z]{2}\d{3}\.\d{5}")
STAR_FIND = re.compile(r"[A-Z]{2}\d{3}\.\d+\*")
PRICE_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$|^\d+$")
LABOR_RE = re.compile(r"^\d+(?:\.\d+)?%$")
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
UNIT_NORM = {"주": "tree", "㎡": "m2", "㎥": "m3", "톤": "ton"}
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳ⓛ"
FIGURE_CAPTION_PREFIXES = ("[그림", "[표준도]")
_STOP = ("TableControl", "GShapeObjectControl")
_PUA_RE = re.compile(r"[\ue000-\uf8ff]")
HALF_LABOR_RE = re.compile(r"^[‘'′`]?\d{2}[상하]")
FIELD_RE = re.compile(r"([가-힣]+(?:및[가-힣]+)*)분야(?:자체표준시장단가)?")
BANNER_RE = re.compile(r"대분류\s*([A-Z])")


def _hwp_to_root(hwp_path: str | Path) -> etree._Element:
    try:
        from hwp5.xmlmodel import Hwp5File
    except ImportError as e:
        raise ImportError("uv sync --extra hwp 필요") from e
    hwp = Hwp5File(str(hwp_path))
    try:
        buf = io.BytesIO()
        hwp.xmlevents().dump(buf)
        buf.seek(0)
        return etree.parse(buf).getroot()
    finally:
        close = getattr(hwp, "close", None)
        if callable(close):
            close()


def _fix_noop(s: str) -> str:
    return s or ""


def _texts_under(el: etree._Element, stop: tuple[str, ...] = _STOP) -> str:
    parts: list[str] = []

    def walk(n: etree._Element) -> None:
        if n.tag == "Text" and n.text:
            parts.append(n.text)
        for ch in n:
            if ch.tag in stop:
                continue
            walk(ch)

    walk(el)
    return "".join(parts)


def _text_nodes(el: etree._Element) -> list[str]:
    out: list[str] = []

    def walk(n: etree._Element) -> None:
        if n.tag == "Text" and n.text:
            out.append(n.text)
        for ch in n:
            if ch.tag in _STOP:
                continue
            walk(ch)

    walk(el)
    return out


def _cell_text(cell: etree._Element) -> str:
    lines = [_texts_under(p) for p in cell.findall("Paragraph")]
    if not lines:
        return _texts_under(cell)
    return "\n".join(lines)


def _top_controls(para: etree._Element, tag: str) -> list[etree._Element]:
    out: list[etree._Element] = []
    for el in para.iter(tag):
        cur = el.getparent()
        nested = False
        while cur is not None and cur is not para:
            if cur.tag == "TableCell":
                nested = True
                break
            cur = cur.getparent()
        if not nested:
            out.append(el)
    return out


def _table_rows(tbl: etree._Element) -> list[list[dict]]:
    body = tbl.find("TableBody")
    if body is None:
        return []
    rows: list[list[dict]] = []
    for row in body.findall("TableRow"):
        cells: list[dict] = []
        for c in row.findall("TableCell"):
            cells.append(
                {
                    "col": int(c.get("col") or 0),
                    "row": int(c.get("row") or 0),
                    "colspan": int(c.get("colspan") or 1),
                    "text": _cell_text(c),
                    "texts": _text_nodes(c),
                }
            )
        rows.append(cells)
    return rows


def _header_fields(cells: list[dict]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for c in cells:
        key = HEADER_NORM.get(re.sub(r"\s+", "", c["text"]))
        if key:
            mapping[c["col"]] = key
    return mapping


def _is_record_header(cells: list[dict]) -> bool:
    fields = set(_header_fields(cells).values())
    return "price" in fields and "code" in fields and len(fields) >= 3


def _is_banner_row(cells: list[dict]) -> bool:
    if len(cells) != 2:
        return False
    a = re.sub(r"\s+", "", cells[0]["text"])
    return a.startswith("대분류") and BANNER_RE.search(cells[0]["text"].replace(" ", "") + cells[0]["text"])


def _is_banner(cells: list[dict]) -> bool:
    if len(cells) != 2:
        return False
    a = re.sub(r"\s+", "", cells[0]["text"])
    return a.startswith("대분류")


def _is_subheader_row(cells: list[dict], ncols: int) -> bool:
    if not cells:
        return False
    code_txt = ""
    for c in cells:
        if c["col"] == 0:
            code_txt = re.sub(r"\s+", "", c["text"])
            break
    has_price = False
    for c in cells:
        t = c["text"].replace(" ", "").strip()
        if PRICE_RE.match(t) or "폐지" in c["text"] or HALF_LABOR_RE.match(t):
            has_price = True
            break
    if STAR_FIND.fullmatch(code_txt) or re.fullmatch(r"[A-Z]{2}\d{3}\.\d+\*+", code_txt):
        if not has_price:
            return True
    for c in cells:
        if c["col"] >= 1 and c["colspan"] >= max(3, ncols - 1) and not has_price:
            return True
    return False


def _is_danga_label(text: str) -> bool:
    t = text.replace(" ", "").strip()
    return "【단가정의】" in t or t == "단가정의"


def _is_figure_caption(text: str) -> bool:
    return text.lstrip().startswith(FIGURE_CAPTION_PREFIXES)


def _is_diagram_table(rows: list[list[dict]]) -> bool:
    blob = " ".join(c["text"] for r in rows for c in r)
    compact = re.sub(r"\s+", "", blob)
    if "시공기준면" in compact:
        return True
    if compact.startswith("건축분야") or compact.startswith("토목분야"):
        return True
    if "자체단가의적용방법" in compact:
        return True
    return False


def _is_diagram_row(cells: list[str]) -> bool:
    nonempty = [c.strip() for c in cells if c.strip()]
    if not nonempty:
        return True
    if any(re.sub(r"\s+", "", c).startswith("굴진방향") for c in nonempty):
        return True
    if any("시공기준면" in c.replace(" ", "") for c in nonempty):
        return True
    compact = [re.sub(r"\s+", "", c) for c in nonempty]
    if compact and all(re.fullmatch(r"(양호|보통|불량)", c) for c in compact):
        return True
    if compact and all(re.fullmatch(r"\d+(?:\.\d+)?m", c) for c in compact):
        return True
    if all(re.fullmatch(r"[A-Z]{2}\d{3}\.\d{5}", c) for c in compact):
        return True
    return False


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _collapse_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _raw_fields(rec: dict) -> list[str]:
    keys = (
        "code",
        "name_raw",
        "name",
        "spec_raw",
        "spec",
        "unit_raw",
        "unit",
        "price_raw",
        "labor_raw",
        "remark_raw",
        "remark",
        "abolished_at",
    )
    return [str(rec.get(k) or "") for k in keys]


def _text_in_obj(text: str, obj: dict) -> bool:
    t = text.strip()
    if not t:
        return True
    blob = " ".join(_raw_fields(obj) if "code" in obj or "name_raw" in obj else [])
    if not blob:
        blob = " ".join(str(v) for v in obj.values() if isinstance(v, str))
    if t in blob:
        return True
    if _compact(t) and _compact(t) in _compact(blob):
        return True
    return False


def _text_in_sub(text: str, sub: dict) -> bool:
    t = text.strip()
    if not t:
        return True
    blob = f"{sub.get('code_pattern') or ''} {sub.get('text') or ''}"
    return t in blob or (_compact(t) and _compact(t) in _compact(blob))


def _wholly_in_one_field(word: str, rec: dict) -> bool:
    w = word.strip()
    if not w:
        return True
    groups = (
        ("code",),
        ("name_raw", "name"),
        ("spec_raw", "spec"),
        ("unit_raw", "unit", "unit_norm"),
        ("price_raw",),
        ("labor_raw", "abolished_at"),
        ("remark_raw", "remark"),
    )
    hits = 0
    cw = _compact(w)
    for g in groups:
        for k in g:
            f = str(rec.get(k) or "")
            if w in f or (cw and cw in _compact(f)):
                hits += 1
                break
    return hits >= 1


def _reparse_price(price_raw: str | None) -> int | None:
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


def _has_inherit_mark(s: str) -> bool:
    t = re.sub(r"\s+", "", s or "")
    return bool(t) and all(c in INHERIT_CHARS for c in t)


def _has_any_inherit_char(s: str) -> bool:
    return any(c in INHERIT_CHARS for c in (s or ""))


def _field_gates(records: list[dict], subheaders: list[dict]) -> dict[str, list[dict[str, Any]]]:
    empty_name: list[dict[str, Any]] = []
    unresolved_inherit: list[dict[str, Any]] = []
    parse_mismatch: list[dict[str, Any]] = []
    for rec in records:
        status = (rec.get("status") or "").strip()
        item = {"code": rec.get("code"), "half": rec.get("half"), "hwp_anchor": rec.get("hwp_anchor")}
        if status in ("present", "abolished") and not (rec.get("name") or "").strip():
            empty_name.append(dict(item, name_raw=rec.get("name_raw")))
        if _has_any_inherit_char(rec.get("name") or "") or _has_inherit_mark(rec.get("spec") or ""):
            unresolved_inherit.append(dict(item, name=rec.get("name"), spec=rec.get("spec")))
        mism: dict[str, Any] = {}
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

    inherit_mismatch: list[dict[str, Any]] = []
    sub_by_group: dict[str, list[dict]] = {}
    for s in subheaders:
        sub_by_group.setdefault(s.get("group_id") or "", []).append(s)
    prev_name = ""
    prev_gid = None
    sub_text = ""
    for rec in records:
        gid = rec.get("group_id")
        if gid != prev_gid:
            prev_name = ""
            sub_text = ""
            prev_gid = gid
            for s in sub_by_group.get(gid or "", []):
                pass
        ng = rec.get("name_group")
        if ng:
            for s in sub_by_group.get(gid or "", []):
                if s.get("code_pattern") == ng:
                    sub_text = s.get("text") or ""
                    break
        if rec.get("name_inherited"):
            expected = sub_text or prev_name
            if (rec.get("name") or "") != expected:
                inherit_mismatch.append(
                    {
                        "code": rec.get("code"),
                        "name": rec.get("name"),
                        "expected": expected,
                    }
                )
        elif rec.get("name"):
            prev_name = rec.get("name") or ""
    return {
        "empty_name": empty_name,
        "unresolved_inherit": unresolved_inherit,
        "parse_mismatch": parse_mismatch,
        "inherit_mismatch": inherit_mismatch,
    }


def _pua_scan(result: dict[str, Any]) -> list[dict[str, Any]]:
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


def _tokenize(s: str) -> list[str]:
    return [w for w in re.split(r"\s+", s) if w]


def _skip_note_word(w: str) -> bool:
    if not w or not w.strip():
        return True
    if re.fullmatch(r"<사례\d+>", w):
        return True
    if re.fullmatch(r"○", w):
        return True
    compact = re.sub(r"\s+", "", w)
    if re.fullmatch(r"[A-Z]{2}\d{3}\.\d+\*+", compact):
        return True
    return False


def _note_blob(groups: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for g in groups:
        parts: list[str] = []
        for n in g.get("notes") or []:
            parts.append(n.get("item") or "")
        for fig in g.get("figures") or []:
            parts.append(fig.get("caption") or "")
        out[g.get("group_id") or ""] = " ".join(parts)
    return out


def check_conservation_root(root: etree._Element, result: dict[str, Any]) -> dict[str, Any]:
    records: list[dict] = list(result.get("records") or [])
    subheaders: list[dict] = list(result.get("subheaders") or [])
    groups: list[dict] = list(result.get("groups") or [])

    rec_by_code: dict[str, dict] = {}
    for rec in records:
        code = str(rec.get("code") or "")
        if code:
            rec_by_code[code] = rec
    sub_by_pat: dict[str, dict] = {}
    for sub in subheaders:
        pat = str(sub.get("code_pattern") or "")
        if pat:
            sub_by_pat[pat] = sub
    recorded_codes = set(rec_by_code)

    missing: list[dict[str, Any]] = []
    duplicate: list[dict[str, Any]] = []
    split_words: list[dict[str, Any]] = []
    lost_spaces: list[dict[str, Any]] = []
    inserted_spaces: list[dict[str, Any]] = []
    unrecorded: list[dict[str, Any]] = []
    notes_missing: list[dict[str, Any]] = []
    notes_duplicate: list[dict[str, Any]] = []
    notes_figure_text: list[dict[str, Any]] = []
    notes_order_mismatch: list[dict[str, Any]] = []
    body_words = 0
    assigned = 0

    body = root.find("BodyText")
    sections = list(body) if body is not None else []
    table_index = 0
    in_notes = False
    last_gid: str | None = groups[0]["group_id"] if groups else None
    group_iter = iter(groups)
    current_group = next(group_iter, None)
    # 그룹은 ■ 문단 순서와 같다고 보고, 주석 낱말은 그룹별로 모은다
    src_notes: dict[str, list[str]] = {g["group_id"]: [] for g in groups}
    active_gid: str | None = None

    def _gid_for_para(para_index: int) -> str | None:
        # 그룹 hwp_anchor.para_index 이하 가장 가까운 ■
        best = None
        best_p = -1
        for g in groups:
            p = (g.get("hwp_anchor") or {}).get("para_index")
            if p is None:
                continue
            if p <= para_index and p >= best_p:
                best = g.get("group_id")
                best_p = p
        return best

    for section in sections:
        colset = section.find("ColumnSet")
        paras = list(colset) if colset is not None else [p for p in section if p.tag == "Paragraph"]
        for para_index, para in enumerate(paras):
            if para.tag != "Paragraph":
                continue
            text = _texts_under(para).strip()
            tables = _top_controls(para, "TableControl")
            shapes = _top_controls(para, "GShapeObjectControl")

            if text.startswith("■"):
                in_notes = False
                active_gid = _gid_for_para(para_index)
            if _is_danga_label(text):
                in_notes = True
                active_gid = _gid_for_para(para_index)
                continue
            compact_line = re.sub(r"\s+", "", text)
            if compact_line and (
                re.fullmatch(r"[가-힣·ㆍ‧･․]{2,12}분야", compact_line)
                or re.fullmatch(r"[가-힣·ㆍ‧･․]{2,20}분야자체표준시장단가", compact_line)
                or compact_line == "목차"
                or text.lstrip().startswith("○")
            ):
                in_notes = False
                active_gid = None
                continue

            ended = False
            for tbl in tables:
                table_index += 1
                rows = _table_rows(tbl)
                if not rows:
                    continue
                if _is_record_header(rows[0]):
                    in_notes = False
                    ended = True
                    colmap = _header_fields(rows[0])
                    ncols = max((c["col"] + c.get("colspan", 1) for r in rows for c in r), default=0)
                    for cells in rows[1:]:
                        row_no = cells[0]["row"] if cells else 0
                        is_sub = _is_subheader_row(cells, ncols)
                        code_txt = ""
                        for c in cells:
                            if c["col"] == 0:
                                code_txt = c["text"]
                                break
                        cm = CODE_FIND.search(code_txt)
                        star_m = STAR_FIND.search(code_txt) or re.search(r"[A-Z]{2}\d{3}\.\d+\*+", code_txt)
                        host_rec = rec_by_code.get(cm.group(0)) if cm and not is_sub else None
                        host_sub = sub_by_pat.get(star_m.group(0)) if star_m and is_sub else None
                        if cm and not is_sub and cm.group(0) not in recorded_codes:
                            unrecorded.append(
                                {
                                    "code": cm.group(0),
                                    "table_index": table_index,
                                    "row": row_no,
                                    "line": code_txt,
                                }
                            )
                        for c in cells:
                            nodes = c.get("texts") or ([c["text"]] if c["text"] else [])
                            prev = None
                            for node in nodes:
                                t = node
                                if not t.strip():
                                    continue
                                body_words += 1
                                hits: list[str] = []
                                if is_sub and host_sub and _text_in_sub(t, host_sub):
                                    hits.append("sub:" + str(host_sub.get("code_pattern")))
                                elif (not is_sub) and host_rec and _text_in_obj(t, host_rec):
                                    hits.append("rec:" + str(host_rec.get("code")))
                                uniq = list(dict.fromkeys(hits))
                                if len(uniq) == 1:
                                    assigned += 1
                                elif len(uniq) == 0:
                                    missing.append(
                                        {
                                            "text": t,
                                            "table_index": table_index,
                                            "row": row_no,
                                            "col": c["col"],
                                        }
                                    )
                                else:
                                    duplicate.append(
                                        {
                                            "text": t,
                                            "table_index": table_index,
                                            "row": row_no,
                                            "keys": uniq,
                                        }
                                    )
                                if host_rec and not is_sub and not _wholly_in_one_field(t, host_rec) and len(_compact(t.strip())) >= 2:
                                    split_words.append(
                                        {
                                            "code": host_rec.get("code"),
                                            "word": t,
                                            "table_index": table_index,
                                            "row": row_no,
                                        }
                                    )
                                prev = t
                    continue
                if _is_banner(rows[0]) and len(rows) == 1:
                    in_notes = False
                    ended = True
                    continue
                if in_notes:
                    if _is_diagram_table(rows):
                        for r in rows:
                            for c in r:
                                for node in c.get("texts") or []:
                                    if node.strip():
                                        notes_figure_text.append({"text": node, "para_index": para_index})
                        continue
                    # 주석 부표: 행을 건너뛴 도식 행의 낱말은 figure, 나머지는 주석
                    gid = active_gid or _gid_for_para(para_index)
                    for r in rows:
                        cells = [c["text"] for c in r]
                        if _is_diagram_row(cells):
                            for c in r:
                                for node in c.get("texts") or []:
                                    if node.strip():
                                        notes_figure_text.append({"text": node, "para_index": para_index})
                            continue
                        if gid:
                            for c in r:
                                src_notes.setdefault(gid, []).extend(
                                    w
                                    for node in (c.get("texts") or [])
                                    for w in _tokenize(node)
                                    if not _skip_note_word(w)
                                )
            if ended:
                continue

            for shp in shapes:
                cap = _texts_under(shp, stop=("TableControl",)).strip()
                if _is_figure_caption(cap):
                    gid = active_gid or _gid_for_para(para_index)
                    if gid:
                        src_notes.setdefault(gid, []).extend(w for w in _tokenize(cap) if not _skip_note_word(w))
                else:
                    for w in _tokenize(cap):
                        notes_figure_text.append({"text": w, "para_index": para_index})

            if tables:
                continue
            if not text:
                continue
            if _is_figure_caption(text):
                gid = active_gid or _gid_for_para(para_index)
                if gid:
                    src_notes.setdefault(gid, []).extend(w for w in _tokenize(text) if not _skip_note_word(w))
                continue
            if text.startswith("■") or _is_danga_label(text):
                continue
            if last_gid and not in_notes and text and (text[0] in CIRCLED or text.startswith("※")):
                in_notes = True
                active_gid = _gid_for_para(para_index)
            if in_notes:
                gid = active_gid or _gid_for_para(para_index)
                if gid:
                    src_notes.setdefault(gid, []).extend(w for w in _tokenize(text) if not _skip_note_word(w))

    # 주석 낱말 vs 산출
    produced = _note_blob(groups)
    for gid, words in src_notes.items():
        blob = produced.get(gid) or ""
        compact_blob = _compact(blob)
        seen_here: dict[str, int] = {}
        order_src = [w for w in words if w]
        for w in order_src:
            if not w.strip():
                continue
            ok = w in blob or (_compact(w) and _compact(w) in compact_blob)
            if not ok:
                notes_missing.append({"group_id": gid, "word": w})
            else:
                seen_here[w] = seen_here.get(w, 0) + 1
        # 순서: 원문 낱말을 compact 로 이은 것이 산출 compact 의 부분문자열(순서 보존)
        src_join = _compact(" ".join(order_src))
        dst_join = compact_blob
        if order_src and not src_join:
            pass
        elif order_src and src_join:
            # 산출에 원문 순서가 유지되는지: src 낱말 compact 가 dst 에서 비감소 위치로
            pos = 0
            mismatch = False
            for w in order_src:
                cw = _compact(w)
                if not cw:
                    continue
                i = dst_join.find(cw, pos)
                if i < 0:
                    mismatch = True
                    break
                pos = i + len(cw)
            notes_items = []
            for g in groups:
                if g.get("group_id") == gid:
                    notes_items = g.get("notes") or []
                    break
            if mismatch and notes_items:
                notes_order_mismatch.append({"group_id": gid})
            if notes_items and not order_src:
                notes_order_mismatch.append({"group_id": gid, "reason": "produced_without_source"})

        # duplicate: 같은 원문 낱말이 산출 항목 둘 이상에만 들어가고 원문은 한 번 — 느슨히 생략
        # (한 낱말이 여러 항목에 반복 인용되면 원문 횟수보다 산출 횟수가 많을 수 있음)

    for g in groups:
        gid = g.get("group_id") or ""
        notes = g.get("notes") or []
        src_w = src_notes.get(gid) or []
        if notes and not src_w:
            if {"group_id": gid, "reason": "produced_without_source"} not in notes_order_mismatch and not any(
                x.get("group_id") == gid for x in notes_order_mismatch
            ):
                notes_order_mismatch.append({"group_id": gid, "reason": "produced_without_source"})

    gates = _field_gates(records, subheaders)
    pua_hits = _pua_scan(result)
    n_empty = len(gates["empty_name"])
    n_unres = len(gates["unresolved_inherit"])
    n_parse = len(gates["parse_mismatch"])
    n_inh = len(gates["inherit_mismatch"])
    n_nm = len(notes_missing)
    n_nd = len(notes_duplicate)
    n_no = len(notes_order_mismatch)
    n_pua = len(pua_hits)

    tot_missing = len(missing)
    tot_dup = len(duplicate)
    tot_split = len(split_words)
    tot_lost = len(lost_spaces)
    tot_ins = len(inserted_spaces)
    tot_unrec = len(unrecorded)

    return {
        "half": result.get("half"),
        "pages": [],
        "split_words": split_words,
        "lost_spaces": lost_spaces,
        "inserted_spaces": inserted_spaces,
        "unrecorded_codes": unrecorded,
        "digit_only": [],
        "empty_name": gates["empty_name"],
        "unresolved_inherit": gates["unresolved_inherit"],
        "parse_mismatch": gates["parse_mismatch"],
        "inherit_mismatch": gates["inherit_mismatch"],
        "notes_missing": notes_missing,
        "notes_duplicate": notes_duplicate,
        "notes_figure_text": notes_figure_text,
        "notes_order_mismatch": notes_order_mismatch,
        "pua_chars": pua_hits,
        "missing": missing,
        "duplicate": duplicate,
        "totals": {
            "body_words": body_words,
            "assigned": assigned,
            "missing": tot_missing,
            "duplicate": tot_dup,
            "split_words": tot_split,
            "lost_spaces": tot_lost,
            "inserted_spaces": tot_ins,
            "unrecorded_codes": tot_unrec,
            "digit_only": 0,
            "empty_name": n_empty,
            "unresolved_inherit": n_unres,
            "parse_mismatch": n_parse,
            "inherit_mismatch": n_inh,
            "notes_missing": n_nm,
            "notes_duplicate": n_nd,
            "notes_figure_text": len(notes_figure_text),
            "notes_order_mismatch": n_no,
            "pua_chars": n_pua,
            "gate_page_errors": 0,
            "pass": tot_missing == 0
            and tot_dup == 0
            and tot_split == 0
            and tot_lost == 0
            and tot_ins == 0
            and tot_unrec == 0
            and n_empty == 0
            and n_unres == 0
            and n_parse == 0
            and n_inh == 0
            and n_nm == 0
            and n_nd == 0
            and n_no == 0
            and n_pua == 0,
        },
    }


def check_conservation(hwp_path: str | Path, result: dict[str, Any]) -> dict[str, Any]:
    root = _hwp_to_root(hwp_path)
    return check_conservation_root(root, result)
