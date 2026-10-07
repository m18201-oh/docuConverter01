"""한글 원본을 olefile 레코드 + kordoc 표로 읽어 pyhwp 와 같은 XML 나무를 만든다.

추출기(`extract_hwp`)만 이 모듈을 쓴다. 보존 검사기는 같은 방법을 자기 파일에 따로 둔다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

from lxml import etree

HWPTAG_PARA_HEADER = 66
HWPTAG_PARA_TEXT = 67
HWPTAG_CTRL_HEADER = 71
HWPTAG_LIST_HEADER = 72
HWPTAG_PAGE_DEF = 73
HWPTAG_TABLE = 77
HWPTAG_EQEDIT = 88

CTRL_TABLE = b" lbt"
CTRL_GSO = b" osg"
CTRL_EQEDIT = b"deqe"

# 1~9, 11~12, 14~23: 8글자. 0, 10, 13, 24~31: 1글자.
_EXT_CTRL = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23})
# TAB(9) 은 8글자이지만 CTRL_HEADER 와 짝이 아니다.
_PAIRED_EXT = _EXT_CTRL - {9}

NODE_REQUIRED_MSG = (
    "Node.js 20 이상이 필요합니다(https://nodejs.org 의 LTS, "
    "Windows 는 winget install OpenJS.NodeJS.LTS)"
)

_VENDOR_KORDOC = Path(__file__).resolve().parent / "vendor" / "kordoc" / "kordoc_bundle.mjs"


class HwpReadError(Exception):
    """한글 원본·kordoc 읽기에서 멈추는 오류."""


def parse_record_header(data: bytes, offset: int = 0) -> tuple[int, int, int, int]:
    """레코드 머리. (tag, level, size, payload_offset). size==0xFFF 이면 다음 4바이트가 크기."""
    if offset + 4 > len(data):
        raise HwpReadError("레코드 머리가 잘렸습니다")
    h = struct.unpack_from("<I", data, offset)[0]
    payload_off = offset + 4
    tag = h & 0x3FF
    level = (h >> 10) & 0x3FF
    size = h >> 20
    if size == 0xFFF:
        if payload_off + 4 > len(data):
            raise HwpReadError("레코드 크기(0xFFF) 다음 4바이트가 없습니다")
        size = struct.unpack_from("<I", data, payload_off)[0]
        payload_off += 4
    return tag, level, size, payload_off


def iter_records(data: bytes) -> list[tuple[int, int, bytes]]:
    out: list[tuple[int, int, bytes]] = []
    off = 0
    n = len(data)
    while off + 4 <= n:
        tag, level, size, payload_off = parse_record_header(data, off)
        end = payload_off + size
        if end > n:
            raise HwpReadError("레코드 본문이 잘렸습니다")
        out.append((tag, level, data[payload_off:end]))
        off = end
    return out


def parse_para_text(payload: bytes) -> tuple[str, int]:
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


def rewrite_cell_markup(s: str) -> str:
    """kordoc 칸 글 표기 → 나무에 넣을 글. 허용되지 않는 표기는 오류."""
    if re.search(r"(?<!\\)\$(?:\\.|[^$\\])+\$", s):
        raise HwpReadError(f"표 칸에 수식($…$)이 있습니다: {s[:80]!r}")
    t = s.replace("\\$", "$").replace("\\<", "<")
    t = t.replace("<sup>2</sup>", "²").replace("<sup>3</sup>", "³")
    if re.search(r"<sup>|<sub>|<u>|~~", t):
        raise HwpReadError(f"표 칸에 다루지 않는 표기가 있습니다: {s[:80]!r}")
    return t


def find_node() -> str | None:
    env = os.environ.get("KEPCO_NODE")
    if env is not None and env != "":
        return env
    return shutil.which("node")


def node_major(path: str) -> int | None:
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


def node_ok() -> bool:
    path = find_node()
    if not path:
        return False
    major = node_major(path)
    return major is not None and major >= 20


def _kordoc_bundle() -> Path:
    return _VENDOR_KORDOC


def run_kordoc(hwp_path: str | Path) -> dict:
    path = find_node()
    if not path or not node_ok():
        raise HwpReadError(NODE_REQUIRED_MSG)
    bundle = _kordoc_bundle()
    if not bundle.is_file():
        raise HwpReadError(f"동봉 kordoc 이 없습니다: {bundle}")
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
            raise HwpReadError(f"kordoc 실행에 실패했습니다: {err[:300]}")
        jp = rec.get("output_json")
        if not jp:
            raise HwpReadError("kordoc 결과에 output_json 이 없습니다")
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
        if tag == HWPTAG_PARA_TEXT:
            text, _n = parse_para_text(payload)
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
                lines.append(rewrite_cell_markup(str(b.get("text") or "")))
        return lines or [""]
    text = rewrite_cell_markup(str((cell or {}).get("text") or ""))
    return text.split("\n") if text else [""]


def _cell_record_texts(nested: list[tuple[int, int, bytes]]) -> list[str]:
    """표 컨트롤 아래 LIST_HEADER(lv2) 칸의 PARA_TEXT. 문단 사이는 줄바꿈."""
    cells: list[str] = []
    current: list[str] | None = None
    for tag, lv, pl in nested:
        if tag == HWPTAG_CTRL_HEADER:
            continue
        if tag == HWPTAG_LIST_HEADER and lv == 2:
            if current is not None:
                cells.append("\n".join(current))
            current = []
            continue
        if current is not None and tag == HWPTAG_PARA_TEXT:
            text, _n = parse_para_text(pl)
            current.append(text)
    if current is not None:
        cells.append("\n".join(current))
    return cells


def _blend_cell_text(rec: str, kor: str) -> str:
    """kordoc 글(위 첨자)을 쓰되, 글자가 같으면 레코드의 공백·줄바꿈을 유지한다."""
    if rec == kor:
        return kor
    rec_n = rec.replace("\n", "")
    kor_n = kor.replace("\n", "")
    if rec_n.strip() == kor_n.strip():
        return rec
    if rec_n.strip().replace("m2", "m²").replace("m3", "m³") == kor_n.strip():
        return rec.replace("m2", "m²").replace("m3", "m³")
    if rec_n.strip() == kor_n.strip().replace("²", "2").replace("³", "3"):
        out = rec
        if "²" in kor:
            out = out.replace("m2", "m²")
        if "³" in kor:
            out = out.replace("m3", "m³")
        return out
    return kor


def _table_control_from_kordoc(tb: dict, rec_texts: list[str] | None = None) -> etree._Element:
    grid = tb.get("cells") or []
    occupied: set[tuple[int, int]] = set()
    tbl = etree.Element("TableControl")
    body = etree.SubElement(tbl, "TableBody")
    emitted = 0
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
            lines = _cell_lines(cell)
            kor = "\n".join(lines)
            if rec_texts is not None and emitted < len(rec_texts):
                kor = _blend_cell_text(rec_texts[emitted], kor)
                lines = kor.split("\n") if kor else [""]
            emitted += 1
            for line in lines:
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
        if tag == HWPTAG_PARA_HEADER and level == 0:
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
    if cid == CTRL_TABLE:
        if table_i[0] >= len(tables):
            raise HwpReadError(
                f"표 컨트롤 수보다 kordoc 표가 적습니다({len(tables)})"
            )
        el = tables[table_i[0]]
        table_i[0] += 1
        return el
    if cid == CTRL_GSO:
        return _gso_el(_gso_text(nested))
    if cid == CTRL_EQEDIT:
        script = ""
        for tag, _lv, pl in nested:
            if tag == HWPTAG_EQEDIT:
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
    text_payloads = [pl for tag, lv, pl in body_recs if tag == HWPTAG_PARA_TEXT and lv == 1]
    headers: list[tuple[bytes, list[tuple[int, int, bytes]]]] = []
    idx = 0
    while idx < len(body_recs):
        tag, lv, pl = body_recs[idx]
        if tag == HWPTAG_CTRL_HEADER and lv == 1:
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
    table_rec_texts: list[list[str]] = []
    i = 0
    while i < len(recs):
        tag, lv, pl = recs[i]
        if tag == HWPTAG_CTRL_HEADER and lv == 1 and _ctrl_id(pl) == CTRL_TABLE:
            table_ctrls += 1
            nested, nxt = _nested(recs, i, 1)
            nrows = None
            for ntag, _nlv, npl in nested:
                if ntag == HWPTAG_TABLE:
                    nrows = _table_n_rows(npl)
                    break
            rec_texts = _cell_record_texts(nested)
            table_rows_rec.append(nrows)
            table_rec_texts.append(rec_texts)
            i = nxt
            continue
        i += 1
    if table_ctrls != len(kordoc_tables):
        raise HwpReadError(
            f"표 컨트롤 수({table_ctrls})와 kordoc 표 수({len(kordoc_tables)})가 다릅니다"
        )
    out: list[etree._Element] = []
    for k, (tb, nrows, rec_texts) in enumerate(zip(kordoc_tables, table_rows_rec, table_rec_texts)):
        krows = len(tb.get("cells") or [])
        if nrows is not None and krows != nrows:
            raise HwpReadError(
                f"{k + 1}번째 표 행 수가 다릅니다(레코드 {nrows} · kordoc {krows})"
            )
        nkor = 0
        occupied: set[tuple[int, int]] = set()
        for r, row in enumerate(tb.get("cells") or []):
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
                nkor += 1
        if len(rec_texts) == nkor + 1:
            rec_texts = rec_texts[1:]
        elif len(rec_texts) != nkor:
            rec_texts = []
        out.append(_table_control_from_kordoc(tb, rec_texts or None))
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


def hwp_to_root(hwp_path: str | Path) -> etree._Element:
    import olefile

    hwp_path = Path(hwp_path)
    kdoc = run_kordoc(hwp_path)
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
        raise HwpReadError("BodyText 섹션이 없습니다")

    for sec_i, data in enumerate(sections):
        recs = iter_records(data)
        tables = _build_tables(recs, ktables if sec_i == 0 else [])
        if sec_i > 0 and any(
            tag == HWPTAG_CTRL_HEADER and lv == 1 and _ctrl_id(pl) == CTRL_TABLE
            for tag, lv, pl in recs
        ):
            # 표는 문서 순서로 한 목록. 섹션이 둘이면 이어 쓴다.
            raise HwpReadError("표가 있는 섹션이 둘 이상입니다")
        table_i = [0]
        section = etree.SubElement(body, "SectionDef")
        section.set("section-id", str(sec_i))
        width, height = 0, 0
        for tag, _lv, pl in recs:
            if tag == HWPTAG_PAGE_DEF:
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
            raise HwpReadError(
                f"표 컨트롤을 다 붙이지 못했습니다({table_i[0]}/{len(tables)})"
            )
    return root
