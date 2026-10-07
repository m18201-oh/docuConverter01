"""HWP 원문 XML 과 산출을 대조하는 보존 게이트.

extract_hwp / extract.py 의 추출 함수를 재사용하지 않는다. XML 을 다시 읽어
레코드 표 셀 Text·주석 문단 낱말을 독립적으로 센다.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from lxml import etree

from .spec import (
    BANNER_RE,
    CIRCLED,
    CODE_FIND,
    DIST_RANGE_RE,
    FIGURE_CAPTION_PREFIXES,
    HALF_LABOR_RE,
    HEADER_MAP,
    HEADER_NORM,
    INHERIT_CHARS,
    LABOR_RE,
    PRICE_RE,
    STAR_FIND,
    UNIT_NORM,
)

_STOP = ("TableControl", "GShapeObjectControl")
_PUA_RE = re.compile(r"[\ue000-\uf8ff]")
FIELD_RE = re.compile(r"([가-힣]+(?:및[가-힣]+)*)분야(?:자체표준시장단가)?")
_SUPER_TRANS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def _eq_text(script: str) -> str:
    s = script.replace("`", "")
    s = s.replace("~", " ")
    s = re.sub(r"(?<![A-Za-z])\s*TIMES\s*(?![A-Za-z])", "×", s)
    s = re.sub(r"\s*\^\{(\d+)\}", lambda m: m.group(1).translate(_SUPER_TRANS), s)
    s = re.sub(r"\s*\^(\d+)", lambda m: m.group(1).translate(_SUPER_TRANS), s)
    s = re.sub(r" {2,}", " ", s).strip()
    if re.search(r"[{}^_#&]", s) or re.search(r"[A-Za-z]{2,}", s):
        return re.sub(r" {2,}", " ", script).strip()
    return s



import json
import os
import shutil
import struct
import subprocess
import tempfile
import zlib

_G_HWPTAG_PARA_HEADER = 66
_G_HWPTAG_PARA_TEXT = 67
_G_HWPTAG_CTRL_HEADER = 71
_G_HWPTAG_PAGE_DEF = 73
_G_HWPTAG_TABLE = 77
_G_HWPTAG_EQEDIT = 88
_G_CTRL_TABLE = b" lbt"
_G_CTRL_GSO = b" osg"
_G_CTRL_EQEDIT = b"deqe"
_EXT_CTRL = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23})
_PAIRED_EXT = _EXT_CTRL - {9}
_GATE_NODE_MSG = (
    "Node.js 20 이상이 필요합니다(https://nodejs.org 의 LTS, "
    "Windows 는 winget install OpenJS.NodeJS.LTS)"
)
_GATE_VENDOR_KORDOC = Path(__file__).resolve().parent / "vendor" / "kordoc" / "kordoc_bundle.mjs"

class _GateHwpError(Exception):
    """한글 원본·kordoc 읽기에서 멈추는 오류."""


def _gate_parse_record_header(data: bytes, offset: int = 0) -> tuple[int, int, int, int]:
    """레코드 머리. (tag, level, size, payload_offset). size==0xFFF 이면 다음 4바이트가 크기."""
    if offset + 4 > len(data):
        raise _GateHwpError("레코드 머리가 잘렸습니다")
    h = struct.unpack_from("<I", data, offset)[0]
    payload_off = offset + 4
    tag = h & 0x3FF
    level = (h >> 10) & 0x3FF
    size = h >> 20
    if size == 0xFFF:
        if payload_off + 4 > len(data):
            raise _GateHwpError("레코드 크기(0xFFF) 다음 4바이트가 없습니다")
        size = struct.unpack_from("<I", data, payload_off)[0]
        payload_off += 4
    return tag, level, size, payload_off


def _gate_iter_records(data: bytes) -> list[tuple[int, int, bytes]]:
    out: list[tuple[int, int, bytes]] = []
    off = 0
    n = len(data)
    while off + 4 <= n:
        tag, level, size, payload_off = _gate_parse_record_header(data, off)
        end = payload_off + size
        if end > n:
            raise _GateHwpError("레코드 본문이 잘렸습니다")
        out.append((tag, level, data[payload_off:end]))
        off = end
    return out


def _gate_parse_para_text(payload: bytes) -> tuple[str, int]:
    """PARA_TEXT → (글, 확장·인라인 컨트롤 수). 문단 끝(13)은 글에 넣지 않는다."""
    if len(payload) % 2:
        payload = payload + b"\x00"
    u = payload.decode("utf-16le", errors="replace")
    chars: list[str] = []
    nctrl = 0
    i = 0
    n = len(u)
    while i < n:
        o = ord(u[i])
        if o < 32:
            if o in _EXT_CTRL:
                nctrl += 1
                i += 8
                continue
            i += 1
            continue
        chars.append(u[i])
        i += 1
    return "".join(chars), nctrl


def _para_events(payload: bytes) -> list[tuple[str, object]]:
    """PARA_TEXT 를 글·짝 있는 확장 컨트롤 사건으로 나눈다."""
    if len(payload) % 2:
        payload = payload + b"\x00"
    u = payload.decode("utf-16le", errors="replace")
    events: list[tuple[str, object]] = []
    buf: list[str] = []
    i = 0
    n = len(u)

    def flush() -> None:
        if buf:
            events.append(("text", "".join(buf)))
            buf.clear()

    while i < n:
        o = ord(u[i])
        if o < 32:
            if o in _EXT_CTRL:
                if o in _PAIRED_EXT:
                    flush()
                    events.append(("ctrl", o))
                i += 8
                continue
            i += 1
            continue
        buf.append(u[i])
        i += 1
    flush()
    return events


def _gate_rewrite_cell_markup(s: str) -> str:
    """kordoc 칸 글 표기 → 나무에 넣을 글. 허용되지 않는 표기는 오류."""
    if re.search(r"(?<!\\)\$(?:\\.|[^$\\])+\$", s):
        raise _GateHwpError(f"표 칸에 수식($…$)이 있습니다: {s[:80]!r}")
    t = s.replace("\\$", "$").replace("\\<", "<")
    t = t.replace("<sup>2</sup>", "²").replace("<sup>3</sup>", "³")
    if re.search(r"<sup>|<sub>|<u>|~~", t):
        raise _GateHwpError(f"표 칸에 다루지 않는 표기가 있습니다: {s[:80]!r}")
    return t


def _gate_find_node() -> str | None:
    env = os.environ.get("KEPCO_NODE")
    if env is not None and env != "":
        return env
    return shutil.which("node")


def _gate_node_major(path: str) -> int | None:
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if raw.startswith("v"):
        raw = raw[1:]
    try:
        return int(raw.split(".")[0])
    except (ValueError, IndexError):
        return None


def _gate_node_ok() -> bool:
    path = _gate_find_node()
    if not path:
        return False
    major = _gate_node_major(path)
    return major is not None and major >= 20


def _kordoc_bundle() -> Path:
    return _GATE_VENDOR_KORDOC


def _gate_run_kordoc(hwp_path: str | Path) -> dict:
    path = _gate_find_node()
    if not path or not _gate_node_ok():
        raise _GateHwpError(_GATE_NODE_MSG)
    bundle = _kordoc_bundle()
    if not bundle.is_file():
        raise _GateHwpError(f"동봉 kordoc 이 없습니다: {bundle}")
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            [path, str(bundle), str(hwp_path), td],
            capture_output=True,
            text=True,
            timeout=300,
        )
        chunks = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        rec = None
        if chunks:
            try:
                rec = json.loads(chunks[-1])
            except json.JSONDecodeError:
                rec = None
        if proc.returncode != 0 or not isinstance(rec, dict):
            err = (proc.stderr or proc.stdout or "").strip() or f"exit={proc.returncode}"
            raise _GateHwpError(f"kordoc 실행에 실패했습니다: {err[:300]}")
        jp = rec.get("output_json")
        if not jp:
            raise _GateHwpError("kordoc 결과에 output_json 이 없습니다")
        return json.loads(Path(jp).read_text(encoding="utf-8"))


def _decompress_stream(data: bytes, compressed: bool) -> bytes:
    if not compressed:
        return data
    return zlib.decompress(data, -15)


def _fileheader_compressed(header: bytes) -> bool:
    if len(header) < 40:
        return False
    return bool(struct.unpack_from("<I", header, 36)[0] & 1)


def _eqedit_script(payload: bytes) -> str:
    if len(payload) < 6:
        return ""
    nchars = struct.unpack_from("<H", payload, 4)[0]
    raw = payload[6 : 6 + nchars * 2]
    return raw.decode("utf-16le", errors="replace")


def _page_def_size(payload: bytes) -> tuple[int, int]:
    if len(payload) < 8:
        return 0, 0
    width, height = struct.unpack_from("<II", payload, 0)
    return int(width), int(height)


def _table_n_rows(payload: bytes) -> int | None:
    if len(payload) < 6:
        return None
    return int(struct.unpack_from("<H", payload, 4)[0])


def _nested(recs: list[tuple[int, int, bytes]], start: int, parent_level: int) -> tuple[list[tuple[int, int, bytes]], int]:
    nested: list[tuple[int, int, bytes]] = []
    j = start + 1
    while j < len(recs) and recs[j][1] > parent_level:
        nested.append(recs[j])
        j += 1
    return nested, j


def _gso_text(nested: list[tuple[int, int, bytes]]) -> str:
    parts: list[str] = []
    for tag, _lv, payload in nested:
        if tag == _G_HWPTAG_PARA_TEXT:
            text, _n = _gate_parse_para_text(payload)
            if text:
                parts.append(text)
    return "".join(parts)


def _kordoc_tables(doc: dict) -> list[dict]:
    tables: list[dict] = []
    for block in doc.get("blocks") or []:
        tb = block.get("table") if isinstance(block, dict) else None
        if tb:
            tables.append(tb)
    return tables


def _cell_lines(cell: dict) -> list[str]:
    blocks = cell.get("blocks") if isinstance(cell, dict) else None
    if blocks:
        lines: list[str] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") in (None, "paragraph") or "text" in b:
                lines.append(_gate_rewrite_cell_markup(str(b.get("text") or "")))
        return lines or [""]
    text = _gate_rewrite_cell_markup(str((cell or {}).get("text") or ""))
    return text.split("\n") if text else [""]


def _table_control_from_kordoc(tb: dict) -> etree._Element:
    grid = tb.get("cells") or []
    occupied: set[tuple[int, int]] = set()
    tbl = etree.Element("TableControl")
    body = etree.SubElement(tbl, "TableBody")
    for r, row in enumerate(grid):
        row_el = etree.SubElement(body, "TableRow")
        if not isinstance(row, list):
            continue
        for c, cell in enumerate(row):
            if (r, c) in occupied:
                continue
            if not isinstance(cell, dict):
                continue
            cs = int(cell.get("colSpan") or 1)
            rs = int(cell.get("rowSpan") or 1)
            for dr in range(rs):
                for dc in range(cs):
                    if dr or dc:
                        occupied.add((r + dr, c + dc))
            cell_el = etree.SubElement(row_el, "TableCell")
            cell_el.set("col", str(c))
            cell_el.set("row", str(r))
            cell_el.set("colspan", str(cs))
            cell_el.set("rowspan", str(rs))
            for line in _cell_lines(cell):
                para = etree.SubElement(cell_el, "Paragraph")
                if line:
                    text_el = etree.SubElement(para, "Text")
                    text_el.text = line
    return tbl


def _eqedit_el(script: str) -> etree._Element:
    el = etree.Element("EqEdit")
    el.set("script", script)
    return el


def _gso_el(text: str) -> etree._Element:
    el = etree.Element("GShapeObjectControl")
    if text:
        t = etree.SubElement(el, "Text")
        t.text = text
    return el


def _split_level0_paras(recs: list[tuple[int, int, bytes]]) -> list[list[tuple[int, int, bytes]]]:
    paras: list[list[tuple[int, int, bytes]]] = []
    cur: list[tuple[int, int, bytes]] | None = None
    for rec in recs:
        tag, level, _payload = rec
        if tag == _G_HWPTAG_PARA_HEADER and level == 0:
            if cur is not None:
                paras.append(cur)
            cur = [rec]
            continue
        if cur is not None:
            cur.append(rec)
    if cur is not None:
        paras.append(cur)
    return paras


def _ctrl_id(payload: bytes) -> bytes:
    return payload[:4] if len(payload) >= 4 else b""


def _emit_ctrl(
    payload: bytes,
    nested: list[tuple[int, int, bytes]],
    tables: list[etree._Element],
    table_i: list[int],
) -> etree._Element | None:
    cid = _ctrl_id(payload)
    if cid == _G_CTRL_TABLE:
        if table_i[0] >= len(tables):
            raise _GateHwpError(
                f"표 컨트롤 수보다 kordoc 표가 적습니다({len(tables)})"
            )
        el = tables[table_i[0]]
        table_i[0] += 1
        return el
    if cid == _G_CTRL_GSO:
        return _gso_el(_gso_text(nested))
    if cid == _G_CTRL_EQEDIT:
        script = ""
        for tag, _lv, pl in nested:
            if tag == _G_HWPTAG_EQEDIT:
                script = _eqedit_script(pl)
                break
        return _eqedit_el(script)
    return None


def _fill_paragraph(
    para: etree._Element,
    body_recs: list[tuple[int, int, bytes]],
    tables: list[etree._Element],
    table_i: list[int],
) -> None:
    text_payloads = [pl for tag, lv, pl in body_recs if tag == _G_HWPTAG_PARA_TEXT and lv == 1]
    headers: list[tuple[bytes, list[tuple[int, int, bytes]]]] = []
    idx = 0
    while idx < len(body_recs):
        tag, lv, pl = body_recs[idx]
        if tag == _G_HWPTAG_CTRL_HEADER and lv == 1:
            nested, nxt = _nested(body_recs, idx, 1)
            headers.append((pl, nested))
            idx = nxt
            continue
        idx += 1

    events: list[tuple[str, object]] = []
    for pl in text_payloads:
        events.extend(_para_events(pl))

    hi = 0
    for kind, val in events:
        if kind == "text":
            s = str(val)
            if s:
                el = etree.SubElement(para, "Text")
                el.text = s
            continue
        if hi >= len(headers):
            continue
        hdr, nested = headers[hi]
        hi += 1
        child = _emit_ctrl(hdr, nested, tables, table_i)
        if child is not None:
            para.append(child)
    while hi < len(headers):
        hdr, nested = headers[hi]
        hi += 1
        child = _emit_ctrl(hdr, nested, tables, table_i)
        if child is not None:
            para.append(child)


def _build_tables(
    recs: list[tuple[int, int, bytes]], kordoc_tables: list[dict]
) -> list[etree._Element]:
    table_ctrls = 0
    table_rows_rec: list[int | None] = []
    i = 0
    while i < len(recs):
        tag, lv, pl = recs[i]
        if tag == _G_HWPTAG_CTRL_HEADER and lv == 1 and _ctrl_id(pl) == _G_CTRL_TABLE:
            table_ctrls += 1
            nested, nxt = _nested(recs, i, 1)
            nrows = None
            for ntag, _nlv, npl in nested:
                if ntag == _G_HWPTAG_TABLE:
                    nrows = _table_n_rows(npl)
                    break
            table_rows_rec.append(nrows)
            i = nxt
            continue
        i += 1
    if table_ctrls != len(kordoc_tables):
        raise _GateHwpError(
            f"표 컨트롤 수({table_ctrls})와 kordoc 표 수({len(kordoc_tables)})가 다릅니다"
        )
    out: list[etree._Element] = []
    for k, (tb, nrows) in enumerate(zip(kordoc_tables, table_rows_rec)):
        krows = len(tb.get("cells") or [])
        if nrows is not None and krows != nrows:
            raise _GateHwpError(
                f"{k + 1}번째 표 행 수가 다릅니다(레코드 {nrows} · kordoc {krows})"
            )
        out.append(_table_control_from_kordoc(tb))
    return out


def _read_section_streams(ole: object, compressed: bool) -> list[bytes]:
    names = []
    for path in ole.listdir():
        if len(path) == 2 and path[0] == "BodyText" and str(path[1]).startswith("Section"):
            names.append(path)
    def _key(p: list) -> tuple[int, str]:
        tail = str(p[1])[7:]
        return (int(tail), str(p[1])) if tail.isdigit() else (10**9, str(p[1]))

    names.sort(key=_key)
    out: list[bytes] = []
    for path in names:
        raw = ole.openstream(path).read()
        out.append(_decompress_stream(raw, compressed))
    return out


def _ole_kordoc_root(hwp_path: str | Path) -> etree._Element:
    import olefile

    hwp_path = Path(hwp_path)
    kdoc = _gate_run_kordoc(hwp_path)
    ktables = _kordoc_tables(kdoc)
    ole = olefile.OleFileIO(str(hwp_path))
    try:
        header = ole.openstream("FileHeader").read()
        compressed = _fileheader_compressed(header)
        sections = _read_section_streams(ole, compressed)
    finally:
        close = getattr(ole, "close", None)
        if callable(close):
            close()

    root = etree.Element("HwpDoc")
    body = etree.SubElement(root, "BodyText")
    if not sections:
        raise _GateHwpError("BodyText 섹션이 없습니다")

    for sec_i, data in enumerate(sections):
        recs = _gate_iter_records(data)
        tables = _build_tables(recs, ktables if sec_i == 0 else [])
        if sec_i > 0 and any(
            tag == _G_HWPTAG_CTRL_HEADER and lv == 1 and _ctrl_id(pl) == _G_CTRL_TABLE
            for tag, lv, pl in recs
        ):
            # 표는 문서 순서로 한 목록. 섹션이 둘이면 이어 쓴다.
            raise _GateHwpError("표가 있는 섹션이 둘 이상입니다")
        table_i = [0]
        section = etree.SubElement(body, "SectionDef")
        section.set("section-id", str(sec_i))
        width, height = 0, 0
        for tag, _lv, pl in recs:
            if tag == _G_HWPTAG_PAGE_DEF:
                width, height = _page_def_size(pl)
                break
        page = etree.SubElement(section, "PageDef")
        page.set("width", str(width))
        page.set("height", str(height))
        colset = etree.SubElement(section, "ColumnSet")
        for para_recs in _split_level0_paras(recs):
            para = etree.SubElement(colset, "Paragraph")
            _fill_paragraph(para, para_recs, tables, table_i)
        if table_i[0] != len(tables) and sec_i == 0:
            raise _GateHwpError(
                f"표 컨트롤을 다 붙이지 못했습니다({table_i[0]}/{len(tables)})"
            )
    return root


def _hwp_to_root(hwp_path: str | Path, warnings: list[str] | None = None) -> etree._Element:
    return _ole_kordoc_root(hwp_path)


def _fix_noop(s: str) -> str:
    return s or ""


def _texts_under(el: etree._Element, stop: tuple[str, ...] = _STOP) -> str:
    parts: list[str] = []

    def walk(n: etree._Element) -> None:
        if n.tag == "Text" and n.text:
            parts.append(n.text)
        elif n.tag == "EqEdit":
            script = n.get("script")
            if script:
                parts.append(_eq_text(script))
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
        elif n.tag == "EqEdit":
            script = n.get("script")
            if script:
                out.append(_eq_text(script))
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


def _nested_in_cell(el: etree._Element, para: etree._Element) -> bool:
    cur = el.getparent()
    while cur is not None and cur is not para:
        if cur.tag == "TableCell":
            return True
        cur = cur.getparent()
    return False


def _inside_tags(el: etree._Element, para: etree._Element, tags: tuple[str, ...]) -> bool:
    cur = el.getparent()
    while cur is not None and cur is not para:
        if cur.tag in tags:
            return True
        cur = cur.getparent()
    return False


def _para_text_table_events(para: etree._Element) -> list[tuple[str, object]]:
    """글 조각과 표(글상자 안 표 포함)를 XML 문서 순서로 낸다."""
    events: list[tuple[str, object]] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            events.append(("text", "".join(buf)))
            buf.clear()

    for n in para.iter():
        if n is para:
            continue
        if n.tag == "TableControl" and not _nested_in_cell(n, para):
            flush()
            events.append(("table", n))
            continue
        if _inside_tags(n, para, _STOP):
            continue
        if n.tag == "Text" and n.text:
            buf.append(n.text)
        elif n.tag == "EqEdit":
            script = n.get("script")
            if script:
                buf.append(_eq_text(script))
    flush()
    return events


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
    price_unparsed: list[dict[str, Any]] = []
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
        if status == "present" and rec.get("price") is None:
            raw = (rec.get("price_raw") or "").strip()
            if raw and "폐지" not in raw:
                price_unparsed.append(dict(item, price_raw=raw))

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
        "price_unparsed": price_unparsed,
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

    def _absorb_note_text(text: str, para_index: int) -> None:
        nonlocal in_notes, active_gid
        if not text:
            return
        if _is_figure_caption(text):
            gid = active_gid or _gid_for_para(para_index)
            if gid:
                src_notes.setdefault(gid, []).extend(w for w in _tokenize(text) if not _skip_note_word(w))
            return
        if text.startswith("■") or _is_danga_label(text):
            return
        if last_gid and not in_notes and text and (text[0] in CIRCLED or text.startswith("※")):
            in_notes = True
            active_gid = _gid_for_para(para_index)
        if in_notes:
            gid = active_gid or _gid_for_para(para_index)
            if gid:
                src_notes.setdefault(gid, []).extend(w for w in _tokenize(text) if not _skip_note_word(w))

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
                if not tables:
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
            mixed_texts = [
                str(payload).strip()
                for kind, payload in (_para_text_table_events(para) if tables else [])
                if kind == "text"
            ]
            if ended:
                for t in mixed_texts:
                    _absorb_note_text(t, para_index)
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
                for t in mixed_texts:
                    _absorb_note_text(t, para_index)
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
    n_price_unparsed = len(gates.get("price_unparsed") or [])
    table_shape_warnings = [
        {"detail": w} for w in (result.get("warnings") or []) if str(w).startswith("table_shape")
    ]

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
        "price_unparsed": gates.get("price_unparsed") or [],
        "table_shape_warnings": table_shape_warnings,
        "field_x_order": [],
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
            "price_unparsed": n_price_unparsed,
            "field_x_order": 0,
            "table_shape_warnings": len(table_shape_warnings),
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
