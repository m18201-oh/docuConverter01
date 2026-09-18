"""HWP 5 바이너리 → 한전 표준시장단가 레코드/그룹/소제목/쪽.

표는 pyhwp XML(`hwp5.xmlmodel`) 경로만 쓴다. 한컴 COM 은 쓰지 않는다.
PDF 경로(`extract.py`)의 추출 함수를 부르지 않는다.
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from lxml import etree

from .extract import ValidationError
from .spec import (
    BANNER_RE,
    BANNER_TRAIL_RE,
    CIRCLED,
    CODE_FIND,
    DIST_RANGE_RE,
    FIELD_RE_HWP as FIELD_RE,
    FIGURE_CAPTION_PREFIXES,
    HALF_FMT_RE,
    HALF_LABOR_RE,
    HEADER_MAP,
    HEADER_NORM,
    INHERIT_CHARS,
    LABOR_RE,
    MAJOR_NAME_MAX_LEN,
    PRICE_RE,
    PUA_DIGIT_MAP as _PUA_DIGIT_MAP,
    PUA_POINT_MAP as _PUA_POINT_MAP,
    PUA_SUPER_MAP as _PUA_SUPER_MAP,
    STAR_FIND,
    UNIT_NORM,
)

_STOP = ("TableControl", "GShapeObjectControl")
_KNOWN_FIELDS = {"토목", "건축", "기계설비및소방설비"}


def _sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fix_pua(s: str) -> str:
    if not s:
        return s
    out: list[str] = []
    for ch in s:
        if ch in _PUA_DIGIT_MAP:
            out.append(_PUA_SUPER_MAP.get(_PUA_DIGIT_MAP[ch], _PUA_DIGIT_MAP[ch]))
        elif ch in _PUA_POINT_MAP:
            out.append(_PUA_POINT_MAP[ch])
        else:
            out.append(ch)
    return "".join(out)


def _validate_half(half: str, src_path: str | Path) -> list[str]:
    if not HALF_FMT_RE.match(half or ""):
        raise ValidationError(f"--half 형식이 올바르지 않습니다(예: 2025H2): {half!r}")
    name = Path(src_path).name
    ym = re.search(r"(20\d{2})", name)
    half_word = "H1" if "상반기" in name else ("H2" if "하반기" in name else None)
    warnings: list[str] = []
    if ym and half_word:
        expected = f"{ym.group(1)}{half_word}"
        if expected != half:
            warnings.append(
                f"--half={half} 이(가) HWP 파일명에서 읽은 반기({expected})와 다릅니다: {name}"
            )
    return warnings


def _texts_under(el: etree._Element, stop: tuple[str, ...] = _STOP) -> str:
    parts: list[str] = []

    def walk(n: etree._Element) -> None:
        if n.tag == "Text" and n.text:
            parts.append(_fix_pua(n.text))
        for ch in n:
            if ch.tag in stop:
                continue
            walk(ch)

    walk(el)
    return "".join(parts)


def _cell_text(cell: etree._Element) -> str:
    lines = [_texts_under(p) for p in cell.findall("Paragraph")]
    if not lines:
        return _texts_under(cell)
    return "\n".join(lines)


def _cell_text_nodes(cell: etree._Element) -> list[str]:
    out: list[str] = []

    def walk(n: etree._Element) -> None:
        if n.tag == "Text" and n.text:
            out.append(_fix_pua(n.text))
        for ch in n:
            if ch.tag in _STOP:
                continue
            walk(ch)

    walk(cell)
    return out


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
                    "rowspan": int(c.get("rowspan") or 1),
                    "text": _cell_text(c),
                    "texts": _cell_text_nodes(c),
                    "el": c,
                }
            )
        rows.append(cells)
    return rows


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


def _is_half_labor(t: str) -> bool:
    return bool(HALF_LABOR_RE.match((t or "").strip().replace(" ", "")))


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
    return a.startswith("대분류")


def _parse_banner(cells: list[dict]) -> tuple[str, str] | None:
    if not _is_banner_row(cells):
        return None
    left = _collapse(cells[0]["text"])
    right = re.sub(r"\s+", "", cells[1]["text"])
    src = left + " " + right
    for cand in (src, re.sub(r"\s+", "", src)):
        m = BANNER_RE.search(cand)
        if m:
            letter = m.group(1)
            name = right or re.sub(r"\s+", "", m.group(2) or "")
            name = BANNER_TRAIL_RE.sub("", name)
            name = re.sub(r"\d+$", "", name)
            if len(name) > MAJOR_NAME_MAX_LEN:
                name = name[:MAJOR_NAME_MAX_LEN]
            return letter, name
    return None


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
        if PRICE_RE.match(t) or "폐지" in c["text"] or _is_half_labor(c["text"]):
            has_price = True
            break
    if STAR_FIND.fullmatch(code_txt) or re.fullmatch(r"[A-Z]{2}\d{3}\.\d+\*+", code_txt):
        if not has_price:
            return True
    for c in cells:
        if c["col"] >= 1 and c["colspan"] >= max(3, ncols - 1) and not has_price:
            return True
    return False


def _is_figure_caption(text: str) -> bool:
    return text.lstrip().startswith(FIGURE_CAPTION_PREFIXES)


def _is_danga_label(text: str) -> bool:
    t = text.replace(" ", "").strip()
    return "【단가정의】" in t or t == "단가정의"


def _parse_field_line(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    m = FIELD_RE.search(compact)
    if not m:
        return None
    name = m.group(1)
    if name == "토건":
        return None
    if name in _KNOWN_FIELDS or name.endswith("설비"):
        return name
    if name in ("토목", "건축"):
        return name
    return None


def _is_diagram_table(rows: list[list[dict]]) -> bool:
    blob = " ".join(c["text"] for r in rows for c in r)
    compact = re.sub(r"\s+", "", blob)
    if "시공기준면" in compact:
        return True
    if compact.startswith("건축분야") or compact.startswith("토목분야"):
        return True
    if "자체단가의적용방법" in compact:
        return True
    # H1: 수량산출 예 굴진방향 도식 — 공종코드+거리구간이고 단가(천단위 쉼표) 없음
    has_price_comma = bool(re.search(r"\d{1,3}(?:,\d{3})+", blob))
    has_code = bool(CODE_FIND.search(blob))
    has_dist = bool(DIST_RANGE_RE.search(blob) or DIST_RANGE_RE.search(compact))
    if has_code and has_dist and not has_price_comma:
        return True
    if "품셈으로산출" in compact and not has_price_comma and (
        has_code or has_dist or any(q in compact for q in ("양호", "보통", "불량"))
    ):
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


def _flatten_subtable(rows: list[list[dict]]) -> str:
    parts: list[str] = []
    for row in rows:
        cells = [_collapse(c["text"].replace("\n", " ")) for c in sorted(row, key=lambda x: x["col"])]
        if _is_diagram_row(cells):
            continue
        if not any(cells):
            continue
        parts.append(" | ".join(cells))
    body = " / ".join(p for p in parts if p.strip("| "))
    return "(표) " + body if body.strip() else ""


def _is_structural_note_junk(s: str) -> bool:
    t = s.strip()
    if not t:
        return True
    if t[0] in CIRCLED or t.startswith("(표)") or t.startswith("※"):
        return False
    compact = t.replace(" ", "")
    if t.startswith("대분류") or compact.startswith("대분류"):
        return True
    if FIELD_RE.search(compact) or "자체표준시장단가" in compact:
        return True
    if re.fullmatch(r"[가-힣·ㆍ‧･․]{2,12}분야", compact):
        return True
    if re.fullmatch(r"-\s*\d+\s*-", t) or re.fullmatch(r"-\d+-", compact):
        return True
    if re.fullmatch(r"\d{4}\.\s*\d{1,2}", t):
        return True
    if STAR_FIND.fullmatch(compact) or re.fullmatch(r"[A-Z]{2}\d+\*+", compact):
        return True
    if re.fullmatch(r"<사례\d+>", compact):
        return True
    if re.match(r"^[가-힣]{1,6}방향\s*→?", t):
        return True
    if re.fullmatch(r"(?:양호|보통|불량)(?:\s+(?:양호|보통|불량))*", t):
        return True
    if re.fullmatch(r"[A-Z]{2}\d{3}\.\d{5}", compact):
        return True
    return False


def _is_note_junk(s: str) -> bool:
    if _is_structural_note_junk(s):
        return True
    t = s.strip()
    if len(t) < 2:
        return True
    if not re.search(r"[가-힣]{2,}", t) and t[0] not in CIRCLED and not t.startswith("※"):
        return True
    return False


def _trim_note(s: str) -> str:
    t = re.sub(r"\s*-\s*\d+\s*-\s*$", "", s.strip())
    return t.strip()


def _note_items(lines: list[str]) -> list[dict]:
    items: list[dict] = []
    parts: list[str] = []

    def flush() -> None:
        if not parts:
            return
        item = _trim_note(" ".join(parts))
        item_raw = _trim_note("\n".join(parts))
        if item and not _is_note_junk(item):
            items.append({"item": item, "item_raw": item_raw, "pdf_page": None})
        parts.clear()

    for line in lines:
        s = line.strip()
        if not s or _is_danga_label(s):
            continue
        if _is_figure_caption(s):
            continue
        if s.startswith("(표)"):
            flush()
            items.append({"item": s, "item_raw": s, "pdf_page": None, "subtable": True})
            continue
        is_new = bool(s) and (s[0] in CIRCLED or s.startswith("※"))
        if is_new:
            flush()
            rest = s[1:].lstrip()
            parts.append(f"{s[0]} {rest}" if rest else s[0])
            continue
        if parts:
            if _is_structural_note_junk(s):
                continue
            parts.append(s)
        else:
            if _is_note_junk(s):
                continue
            parts.append(s)
    flush()
    return items


def _merge_note_continuations(notes: list[dict]) -> list[dict]:
    merged: list[dict] = []
    last_text_idx: int | None = None
    for n in notes:
        item = n.get("item", "")
        is_boundary = bool(item) and (item[0] in CIRCLED or item.startswith("※"))
        is_subtable = bool(n.get("subtable")) or item.startswith("(표)") or item.startswith("[표]")
        if is_boundary or is_subtable or last_text_idx is None:
            merged.append(dict(n))
            if is_boundary:
                last_text_idx = len(merged) - 1
            elif not is_subtable:
                last_text_idx = len(merged) - 1
            continue
        target = merged[last_text_idx]
        target["item"] = _trim_note(f"{target['item']} {item}")
        target["item_raw"] = f"{target.get('item_raw', target['item'])}\n{n.get('item_raw', item)}"
    return merged


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


def _anchor(section: int, para_index: int, table_index: int | None, row: int | None) -> dict:
    return {
        "section": section,
        "para_index": para_index,
        "table_index": table_index,
        "row": row,
    }


def extract_from_root(
    root: etree._Element,
    half: str,
    sha: str,
    source_path: str | Path | None = None,
) -> dict:
    warnings: list[str] = []
    if source_path is not None:
        warnings.extend(_validate_half(half, source_path))
    elif not HALF_FMT_RE.match(half or ""):
        raise ValidationError(f"--half 형식이 올바르지 않습니다(예: 2025H2): {half!r}")

    body = root.find("BodyText")
    if body is None:
        raise ValidationError("HWP XML 에 BodyText 가 없습니다")
    sections = list(body)
    if not sections:
        raise ValidationError("HWP XML 에 SectionDef 가 없습니다")

    records: list[dict] = []
    groups: list[dict] = []
    subheaders: list[dict] = []
    pages_out: list[dict] = []

    last_group: dict | None = None
    last_major = ""
    last_major_name = ""
    last_field = ""
    last_sub: tuple[str, str] | None = None
    last_prev_name = ""
    in_notes = False
    note_lines: list[str] = []
    group_seq = 0
    table_index = 0

    def flush_notes() -> None:
        nonlocal in_notes, note_lines
        if last_group is not None and note_lines:
            items = _note_items(note_lines)
            if items:
                last_group["notes"].extend(items)
        note_lines = []
        in_notes = False

    def new_group(header_raw: str, section_id: int, para_index: int) -> dict:
        nonlocal group_seq, last_group, last_sub, last_prev_name
        flush_notes()
        group_seq += 1
        last_sub = None
        last_prev_name = ""
        raw = header_raw.strip()
        if raw.startswith("■") and len(raw) > 1 and raw[1] != " ":
            raw = "■ " + raw[1:]
        if not raw.startswith("■"):
            raw = "■ " + raw
        header = raw.lstrip("■").strip()
        gid = f"{half}#h{para_index}#{group_seq}"
        g = {
            "group_id": gid,
            "half": half,
            "pdf_page": None,
            "page_half": None,
            "printed_page": None,
            "header_raw": raw,
            "header": header,
            "major": last_major,
            "major_name": last_major_name,
            "field": last_field,
            "notes": [],
            "figures": [],
            "record_count": 0,
            "hwp_anchor": _anchor(section_id, para_index, None, None),
        }
        groups.append(g)
        last_group = g
        return g

    def add_figure(caption: str) -> None:
        cap = caption.strip()
        if not cap or last_group is None:
            return
        last_group["figures"].append({"caption": cap, "pdf_page": None})

    def handle_record_table(
        rows: list[list[dict]],
        section_id: int,
        para_index: int,
        t_index: int,
    ) -> None:
        nonlocal last_sub, last_prev_name, last_group
        flush_notes()
        header = rows[0]
        colmap = _header_fields(header)
        ncols = max((c["col"] + c["colspan"] for r in rows for c in r), default=0)
        grp = last_group
        current_sub = last_sub
        prev_name = last_prev_name
        has_remark = "remark" in colmap.values()
        if ncols >= 8:
            warnings.append(
                f"table_shape section={section_id} para={para_index}: record_table_cols={ncols} (>=8)"
            )
        if ncols <= 3:
            warnings.append(
                f"table_shape section={section_id} para={para_index}: record_table_cols={ncols} (<=3) mapped={sorted(colmap.values())}"
            )

        for cells in rows[1:]:
            row_no = cells[0]["row"] if cells else 0
            if _is_subheader_row(cells, ncols):
                code_pat = ""
                text = ""
                for c in cells:
                    compact = re.sub(r"\s+", "", c["text"])
                    if STAR_FIND.fullmatch(compact) or re.fullmatch(r"[A-Z]{2}\d{3}\.\d+\*+", compact):
                        code_pat = compact
                    elif c["col"] >= 1 and c["text"].strip():
                        text = _collapse(c["text"])
                if not code_pat:
                    for c in cells:
                        m = STAR_FIND.search(c["text"])
                        if m:
                            code_pat = m.group(0)
                            rest = STAR_FIND.sub("", c["text"])
                            text = _collapse(rest) or text
                            break
                if code_pat:
                    current_sub = (code_pat, text or code_pat)
                    subheaders.append(
                        {
                            "half": half,
                            "pdf_page": None,
                            "page_half": None,
                            "y0": None,
                            "code_pattern": code_pat,
                            "text": text or code_pat,
                            "group_id": grp["group_id"] if grp else None,
                            "hwp_anchor": _anchor(section_id, para_index, t_index, row_no),
                        }
                    )
                continue

            fields: dict[str, str] = {name: "" for name in ("code", "name", "spec", "unit", "price", "labor", "remark")}
            for c in cells:
                fname = colmap.get(c["col"])
                if not fname:
                    continue
                fields[fname] = c["text"]

            code_raw = fields.get("code") or ""
            m = CODE_FIND.search(code_raw)
            if not m:
                continue
            code = m.group(0)
            name_raw = fields.get("name") or ""
            spec_raw = fields.get("spec") or ""
            unit_raw = fields.get("unit") or ""
            price_raw0 = fields.get("price") or ""
            labor_raw0 = fields.get("labor") or ""
            remark_raw = fields.get("remark") or ""

            if name_raw.startswith(code):
                name_raw = name_raw[len(code) :].lstrip()

            unit = _collapse(unit_raw)
            if "\n" in unit_raw:
                unit = _collapse(unit_raw.split("\n")[-1])

            price_tok = ""
            for line in (price_raw0 or "").split("\n"):
                t = line.strip().replace(" ", "")
                if "폐지" in t:
                    price_tok = "폐지"
                    break
                if PRICE_RE.match(t):
                    price_tok = t
                    break
            labor_tok = ""
            for line in (labor_raw0 or "").split("\n"):
                t = line.strip().replace(" ", "")
                if LABOR_RE.match(t) or _is_half_labor(t):
                    labor_tok = line.strip()
                    break
            if not labor_tok:
                labor_tok = _collapse(labor_raw0)

            price_raw, price, status = _parse_price(price_tok or price_raw0)
            labor_raw, labor_ratio = _parse_labor(labor_tok)
            if status == "present" and price is None and _is_half_labor(labor_tok or labor_raw0):
                raw = (price_tok or price_raw0 or "").strip()
                if raw:
                    price_raw, price, status = raw, None, "abolished"
            abolished_at = None
            if status == "abolished":
                labor_ratio = None
                abolished_at = labor_raw
                if not abolished_at:
                    abolished_at = labor_tok

            name_blank = not re.sub(r"\s+", "", name_raw or "")
            inherited = _is_inherit(name_raw) or name_blank
            name_group = current_sub[0] if current_sub else None
            if inherited:
                name = current_sub[1] if current_sub else prev_name
                if not name:
                    warnings.append(
                        f"명칭 상속 실패(이어받을 명칭 없음): code={code} half={half} name_raw={name_raw!r}"
                    )
            else:
                name = _collapse(name_raw)
                prev_name = name

            spec = _collapse(spec_raw) if spec_raw.strip() else spec_raw.strip()

            if status == "present" and price is None and "폐지" not in (price_tok or ""):
                raw_keep = (price_tok or price_raw or "").strip()
                if not raw_keep:
                    continue

            rec = {
                "key": f"{code}@{half}",
                "code": code,
                "half": half,
                "major": code[0],
                "field": last_field,
                "pdf_page": None,
                "page_half": None,
                "printed_page": None,
                "group_id": grp["group_id"] if grp else None,
                "name_group": name_group if inherited or name_group else None,
                "bbox": None,
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
                "hwp_anchor": _anchor(section_id, para_index, t_index, row_no),
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

        last_sub = current_sub
        last_prev_name = prev_name

    for sec_i, section in enumerate(sections):
        section_id = int(section.get("section-id") or sec_i)
        page_def = section.find("PageDef")
        width = int(page_def.get("width") or 0) if page_def is not None else 0
        height = int(page_def.get("height") or 0) if page_def is not None else 0
        colset = section.find("ColumnSet")
        paras = list(colset) if colset is not None else [p for p in section if p.tag == "Paragraph"]

        for para_index, para in enumerate(paras):
            if para.tag != "Paragraph":
                continue
            text = _texts_under(para)
            text_s = text.strip()
            tables = _top_controls(para, "TableControl")
            shapes = _top_controls(para, "GShapeObjectControl")

            field_hit = _parse_field_line(text_s) if text_s else None
            if field_hit:
                if last_field and field_hit != last_field:
                    flush_notes()
                    last_group = None
                    last_major = ""
                    last_major_name = ""
                last_field = field_hit

            for tbl in tables:
                table_index += 1
                rows = _table_rows(tbl)
                if not rows:
                    continue
                if _is_record_header(rows[0]):
                    handle_record_table(rows, section_id, para_index, table_index)
                    continue
                banner = _parse_banner(rows[0]) if len(rows) == 1 else None
                if banner:
                    flush_notes()
                    last_major, last_major_name = banner
                    continue
                if in_notes or (last_group is not None and not _is_diagram_table(rows)):
                    if _is_diagram_table(rows):
                        continue
                    flat = _flatten_subtable(rows)
                    if flat:
                        if not in_notes:
                            in_notes = True
                        note_lines.append(flat)
                # 그 외(목차·표지 표)는 버린다

            for shp in shapes:
                cap = _texts_under(shp, stop=("TableControl",)).strip()
                if _is_figure_caption(cap):
                    add_figure(cap)

            if tables:
                continue

            if not text_s:
                continue
            if _is_figure_caption(text_s):
                add_figure(text_s)
                continue
            if text_s.startswith("■"):
                new_group(text_s, section_id, para_index)
                continue
            if _is_danga_label(text_s):
                in_notes = True
                continue
            if last_group is not None and not in_notes and text_s and (
                text_s[0] in CIRCLED or text_s.startswith("※")
            ):
                in_notes = True
            if in_notes:
                note_lines.append(text_s)

        pages_out.append(
            {
                "pdf_page": None,
                "layout": "hwp",
                "printed": {},
                "width": width,
                "height": height,
                "field": None,
                "section": section_id,
            }
        )

    flush_notes()
    for g in groups:
        if g.get("notes"):
            g["notes"] = _merge_note_continuations(g["notes"])

    return {
        "records": records,
        "groups": groups,
        "subheaders": subheaders,
        "pages": pages_out,
        "sha256": sha,
        "half": half,
        "warnings": warnings,
    }


def extract_hwp(hwp_path: str | Path, half: str) -> dict:
    hwp_path = Path(hwp_path)
    if not hwp_path.exists():
        raise ValidationError(f"HWP 파일이 없습니다: {hwp_path}")
    try:
        size = hwp_path.stat().st_size
    except OSError as e:
        raise ValidationError(f"HWP 파일을 읽을 수 없습니다: {hwp_path}") from e
    if size == 0:
        raise ValidationError(f"HWP 파일이 비어 있습니다: {hwp_path}")
    warnings = _validate_half(half, hwp_path)
    sha = _sha256(hwp_path)
    try:
        root = _hwp_to_root(hwp_path)
    except ValidationError:
        raise
    except Exception as e:
        raise ValidationError(f"손상된 HWP 이거나 열 수 없습니다: {hwp_path}") from e
    body = root.find("BodyText")
    text_blob = "".join(body.itertext()) if body is not None else ""
    if not re.search(r"[가-힣A-Za-z0-9]", text_blob or ""):
        raise ValidationError(f"글자층이 없는 HWP 입니다: {hwp_path}")
    result = extract_from_root(root, half, sha, source_path=hwp_path)
    # _validate_half 을 extract_from_root 에서도 호출하므로 중복 경고를 접는다
    seen: set[str] = set()
    merged: list[str] = []
    for w in list(warnings) + list(result.get("warnings") or []):
        if w not in seen:
            seen.add(w)
            merged.append(w)
    result["warnings"] = merged
    return result
