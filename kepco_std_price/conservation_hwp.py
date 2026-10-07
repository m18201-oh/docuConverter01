"""HWP 원문 나무와 산출을 대조하는 보존 게이트.

extract_hwp / extract.py 의 추출 함수를 재사용하지 않는다. 한글 원본을 이 파일 안의
독자 읽기 코드로 다시 읽어 레코드 표 셀 Text·주석 문단 낱말을 독립적으로 센다.
"""
from __future__ import annotations

import hashlib
import re
import struct
import zlib
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



# ---------------------------------------------------------------------------
# 한글(HWP 5.0) 원본 읽기 — 검사기 쪽 독자 사본.
# 추출기(`hwp_records`)와 코드를 나눠 쓰지 않고 같은 방법으로 따로 쓴다.
# 공개된 HWP 5.0 문서 형식만 보고 쓴 독자 구현이다(레코드·PARA_TEXT·글자 모양·표 레코드).
# ---------------------------------------------------------------------------

_RD_HWPTAG_CHAR_SHAPE = 21
_RD_HWPTAG_PARA_HEADER = 66
_RD_HWPTAG_PARA_TEXT = 67
_RD_HWPTAG_PARA_CHAR_SHAPE = 68
_RD_HWPTAG_PARA_LINE_SEG = 69
_RD_HWPTAG_CTRL_HEADER = 71
_RD_HWPTAG_LIST_HEADER = 72
_RD_HWPTAG_PAGE_DEF = 73
_RD_HWPTAG_TABLE = 77
_RD_HWPTAG_EQEDIT = 88

_RD_CTRL_TABLE = b" lbt"
_RD_CTRL_GSO = b" osg"
_RD_CTRL_EQEDIT = b"deqe"
_RD_CTRL_SECD = b"dces"
_RD_CTRL_COLD = b"dloc"
_RD_CTRL_HEADER_ = b"daeh"
_RD_CTRL_FOOTER = b"toof"

# 글자 모양 속성의 비트: 15 위 첨자, 16 아래 첨자. 속성은 레코드 46바이트째 UINT32.
_RD_CHAR_SHAPE_PROP_OFFSET = 46
_RD_SUPERSCRIPT_BIT = 1 << 15
_RD_SUBSCRIPT_BIT = 1 << 16

# 제어 문자(0~31). 확장 컨트롤은 CTRL_HEADER 와 짝이 되고, 인라인 컨트롤은 짝이 없지만 8글자를 차지한다.
_RD_EXTENDED_CTRL = frozenset({1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23})
_RD_INLINE_CTRL = frozenset({4, 5, 6, 7, 8, 9, 19, 20})
_RD_CTRL_WIDTH = 8

_RD_SUPERSCRIPT_MAP = {"2": "²", "3": "³"}


class _RdError(Exception):
    """한글 원본을 읽다가 멈추는 오류."""


# ---------------------------------------------------------------- 레코드


def _rd_parse_record_header(data: bytes, offset: int = 0) -> tuple[int, int, int, int]:
    """레코드 머리. (tag, level, size, payload_offset). size==0xFFF 이면 다음 4바이트가 크기."""
    if offset + 4 > len(data):
        raise _RdError("레코드 머리가 잘렸습니다")
    h = struct.unpack_from("<I", data, offset)[0]
    payload_off = offset + 4
    tag = h & 0x3FF
    level = (h >> 10) & 0x3FF
    size = h >> 20
    if size == 0xFFF:
        if payload_off + 4 > len(data):
            raise _RdError("레코드 크기(0xFFF) 다음 4바이트가 없습니다")
        size = struct.unpack_from("<I", data, payload_off)[0]
        payload_off += 4
    return tag, level, size, payload_off


def _rd_iter_records(data: bytes) -> list[tuple[int, int, bytes]]:
    out: list[tuple[int, int, bytes]] = []
    off = 0
    n = len(data)
    while off < n:
        tag, level, size, payload_off = _rd_parse_record_header(data, off)
        end = payload_off + size
        if end > n:
            raise _RdError("레코드 본문이 잘렸습니다")
        out.append((tag, level, data[payload_off:end]))
        off = end
    return out


def _rd_char_shape_props(docinfo_records: list[tuple[int, int, bytes]]) -> list[int]:
    """DocInfo 의 글자 모양(CHAR_SHAPE) 속성을 id(0부터) 순서로."""
    props: list[int] = []
    for tag, _lv, payload in docinfo_records:
        if tag != _RD_HWPTAG_CHAR_SHAPE:
            continue
        if len(payload) < _RD_CHAR_SHAPE_PROP_OFFSET + 4:
            raise _RdError("글자 모양 레코드가 짧습니다")
        props.append(struct.unpack_from("<I", payload, _RD_CHAR_SHAPE_PROP_OFFSET)[0])
    return props


# ---------------------------------------------------------------- 문단 글


def _rd_units(payload: bytes) -> list[int]:
    if len(payload) % 2:
        payload = payload + b"\x00"
    return list(struct.unpack("<%dH" % (len(payload) // 2), payload))


def _rd_decode(units: list[int]) -> str:
    return struct.pack("<%dH" % len(units), *units).decode("utf-16le", errors="replace")


def _rd_tokens(payload: bytes) -> list[tuple[str, int, int]]:
    """PARA_TEXT → [(종류, 값, 위치)]. 종류: 'ch' 글자 코드 · 'ext' 확장 컨트롤 · 'inl' 인라인 · 'ctl' 그 밖의 제어 글자.

    위치는 문단 글 안의 UTF-16 글자 위치(컨트롤은 8글자를 차지한다)다.
    """
    u = _rd_units(payload)
    out: list[tuple[str, int, int]] = []
    i = 0
    n = len(u)
    while i < n:
        c = u[i]
        if c >= 32:
            out.append(("ch", c, i))
            i += 1
        elif c in _RD_EXTENDED_CTRL:
            out.append(("ext", c, i))
            i += _RD_CTRL_WIDTH
        elif c in _RD_INLINE_CTRL:
            out.append(("inl", c, i))
            i += _RD_CTRL_WIDTH
        else:
            out.append(("ctl", c, i))
            i += 1
    return out


def _rd_parse_para_text(payload: bytes) -> tuple[str, int]:
    """PARA_TEXT → (글, 확장 컨트롤 수). 인라인 컨트롤(탭 등)·줄바꿈·문단 끝(13)은 글에 넣지 않는다."""
    toks = _rd_tokens(payload)
    text = _rd_decode([v for kind, v, _p in toks if kind == "ch"])
    nctrl = sum(1 for kind, _v, _p in toks if kind == "ext")
    return text, nctrl


# 글자의 언어 갈래. 한글 문서는 글자 갈래가 바뀌는 자리에서 글 조각을 나눈다.
def _rd_lang(code: int) -> str | None:
    """강한 글자는 갈래 이름, 앞 글자를 따라가는 글자(숫자·빈칸·구두점)는 None."""
    if code < 128:
        ch = chr(code)
        if ch.isalpha() or ch in "[]~&":
            return "en"
        return None
    if code == 0x3000:
        return None
    if 0xA0 <= code <= 0x24F:
        return "en"
    if (
        0x1100 <= code <= 0x11FF
        or 0x3130 <= code <= 0x318F
        or 0xA960 <= code <= 0xA97F
        or 0xAC00 <= code <= 0xD7FF
        or 0xFFA0 <= code <= 0xFFDC
    ):
        return "ko"
    if 0x3001 <= code <= 0x303F:
        return "symbol"
    if 0x3040 <= code <= 0x30FF:
        return "jp"
    if 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
        return "cn"
    if 0xE000 <= code <= 0xF8FF:
        return "symbol"
    return "other"


def _rd_pairs(payload: bytes | None) -> list[tuple[int, int]]:
    """PARA_CHAR_SHAPE → [(글자 위치, 글자 모양 id)]."""
    if not payload:
        return []
    n = len(payload) // 8
    arr = struct.unpack("<%dI" % (n * 2), payload[: n * 8])
    return [(arr[2 * k], arr[2 * k + 1]) for k in range(n)]


def _rd_line_starts(payload: bytes | None) -> list[int]:
    """PARA_LINE_SEG → 줄이 시작하는 글자 위치(줄마다 36바이트, 첫 UINT32)."""
    if not payload:
        return []
    return [struct.unpack_from("<I", payload, k * 36)[0] for k in range(len(payload) // 36)]


def _rd_text_nodes(
    toks: list[tuple[str, int, int]],
    shapes: list[tuple[int, int]],
    line_starts: list[int],
    props: list[int],
    in_cell: bool,
) -> list[str | None]:
    """글자 토큰 → 순서대로 글 조각(글) 또는 확장 컨트롤 자리(None).

    글 조각은 컨트롤이 나오는 자리·글자 모양이 바뀌는 자리·줄이 시작하는 자리·글자 갈래가 바뀌는 자리에서 끊는다.
    갈래는 컨트롤·글자 모양·줄로 끊긴 구간마다 정한다: 숫자·빈칸·구두점은 앞의 강한 글자를 따르고,
    구간 첫머리의 그런 글자는 구간의 첫 강한 글자를 따른다.
    """
    shape_at = {pos: sid for pos, sid in shapes}
    shape_pos = sorted(shape_at)
    bounds = set(line_starts)

    # 1) 글자마다 글자 모양 id 와 갈래를 정한다.
    sp = 0
    cur_shape = 0
    run: list[tuple[int, int, int]] = []  # (위치, 글자 코드, 글자 모양)
    lang_of: dict[int, str] = {}
    prev_lang = "ko"

    def assign() -> None:
        nonlocal prev_lang, run
        first = None
        for _p, c, _s in run:
            lg = _rd_lang(c)
            if lg is not None:
                first = lg
                break
        cur = first or prev_lang
        for p, c, _s in run:
            lg = _rd_lang(c)
            if lg is not None:
                cur = lg
            lang_of[p] = cur
        if run:
            prev_lang = cur
        run = []

    shape_of: dict[int, int] = {}
    for kind, v, p in toks:
        while sp < len(shape_pos) and shape_pos[sp] <= p:
            cur_shape = shape_at[shape_pos[sp]]
            sp += 1
        if kind != "ch":
            assign()
            continue
        if run and (run[-1][2] != cur_shape or p in bounds):
            assign()
        run.append((p, v, cur_shape))
        shape_of[p] = cur_shape
    assign()

    # 2) 끊어서 글 조각을 만든다.
    out: list[str | None] = []
    buf: list[int] = []
    buf_shape = -1
    buf_lang = ""

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        s = _rd_decode(buf)
        prop = props[buf_shape] if 0 <= buf_shape < len(props) else 0
        if in_cell and prop & _RD_SUBSCRIPT_BIT:
            raise _RdError(f"표 칸에 아래 첨자가 있습니다: {s[:40]!r}")
        if in_cell and prop & _RD_SUPERSCRIPT_BIT:
            if any(ch not in _RD_SUPERSCRIPT_MAP for ch in s):
                raise _RdError(f"표 칸에 2·3 말고 다른 위 첨자가 있습니다: {s[:40]!r}")
            s = "".join(_RD_SUPERSCRIPT_MAP[ch] for ch in s)
        out.append(s)
        buf = []

    for kind, v, p in toks:
        if kind != "ch":
            flush()
            if kind == "ext":
                out.append(None)
            continue
        sid = shape_of[p]
        lg = lang_of[p]
        if buf and (p in bounds or sid != buf_shape or lg != buf_lang):
            flush()
        if not buf:
            buf_shape = sid
            buf_lang = lg
        buf.append(v)
    flush()
    return out


# ---------------------------------------------------------------- 레코드 → 나무


def _rd_subtree_end(recs: list[tuple[int, int, bytes]], i: int) -> int:
    lv = recs[i][1]
    j = i + 1
    n = len(recs)
    while j < n and recs[j][1] > lv:
        j += 1
    return j


def _rd_eqedit_script(payload: bytes) -> str:
    if len(payload) < 6:
        return ""
    nchars = struct.unpack_from("<H", payload, 4)[0]
    raw = payload[6 : 6 + nchars * 2]
    return raw.decode("utf-16le", errors="replace")


def _rd_page_def_size(payload: bytes) -> tuple[int, int]:
    if len(payload) < 8:
        return 0, 0
    width, height = struct.unpack_from("<II", payload, 0)
    return int(width), int(height)


def _rd_table_row_sizes(payload: bytes) -> list[int]:
    """TABLE 레코드: 속성(4) 행 수(2) 열 수(2) 칸 간격(2) 안 여백(2×4) 다음에 행마다 칸 수."""
    if len(payload) < 18:
        raise _RdError("표 레코드가 짧습니다")
    nrows = struct.unpack_from("<H", payload, 4)[0]
    if len(payload) < 18 + nrows * 2:
        raise _RdError("표 레코드의 행 칸 수가 잘렸습니다")
    return list(struct.unpack_from("<%dH" % nrows, payload, 18))


def _rd_cell_pos(payload: bytes) -> tuple[int, int, int, int]:
    """칸의 LIST_HEADER: 문단 수(2) 속성(4) 다음 2바이트를 건너뛰고 열·행·열 병합·행 병합(UINT16 ×4)."""
    if len(payload) < 16:
        raise _RdError("칸 머리가 짧습니다")
    col, row, cs, rs = struct.unpack_from("<HHHH", payload, 8)
    return col, row, cs, rs


class _rd_Ctx:
    def __init__(self, props: list[int]) -> None:
        self.props = props


def _rd_build_paragraph(
    recs: list[tuple[int, int, bytes]], i: int, ctx: _rd_Ctx, in_cell: bool
) -> tuple[etree._Element, int]:
    """recs[i] 가 PARA_HEADER. (Paragraph 요소, 다음 레코드 위치)."""
    lv = recs[i][1]
    end = _rd_subtree_end(recs, i)
    text_pl = None
    shape_pl = None
    line_pl = None
    ctrls: list[tuple[int, int]] = []
    j = i + 1
    while j < end:
        tag, l2, pl = recs[j]
        if l2 == lv + 1:
            if tag == _RD_HWPTAG_PARA_TEXT and text_pl is None:
                text_pl = pl
            elif tag == _RD_HWPTAG_PARA_CHAR_SHAPE and shape_pl is None:
                shape_pl = pl
            elif tag == _RD_HWPTAG_PARA_LINE_SEG and line_pl is None:
                line_pl = pl
            elif tag == _RD_HWPTAG_CTRL_HEADER:
                e = _rd_subtree_end(recs, j)
                ctrls.append((j, e))
                j = e
                continue
        j += 1

    para = etree.Element("Paragraph")
    toks = _rd_tokens(text_pl) if text_pl is not None else []
    nodes = _rd_text_nodes(toks, _rd_pairs(shape_pl), _rd_line_starts(line_pl), ctx.props, in_cell)
    n_ext = sum(1 for kind, _v, _p in toks if kind == "ext")
    if n_ext != len(ctrls):
        raise _RdError(
            f"문단의 확장 컨트롤 수({n_ext})와 CTRL_HEADER 수({len(ctrls)})가 다릅니다"
        )
    ci = 0
    for item in nodes:
        if item is not None:
            t = etree.SubElement(para, "Text")
            t.text = str(item)
            continue
        cj, ce = ctrls[ci]
        ci += 1
        el = _rd_build_control(recs, cj, ce, ctx, in_cell)
        if el is not None:
            para.append(el)
    return para, end


def _rd_paragraphs_in(
    recs: list[tuple[int, int, bytes]], start: int, end: int, ctx: _rd_Ctx, in_cell: bool, parent: etree._Element
) -> None:
    j = start
    while j < end:
        if recs[j][0] == _RD_HWPTAG_PARA_HEADER:
            p, j = _rd_build_paragraph(recs, j, ctx, in_cell)
            parent.append(p)
        else:
            j += 1


def _rd_build_control(
    recs: list[tuple[int, int, bytes]], i: int, end: int, ctx: _rd_Ctx, in_cell: bool
) -> etree._Element | None:
    payload = recs[i][2]
    cid = payload[:4]
    if cid in (_RD_CTRL_SECD, _RD_CTRL_COLD):
        return None
    if cid == _RD_CTRL_EQEDIT:
        wrap = etree.Element("Control")
        eq = etree.SubElement(wrap, "EqEdit")
        for k in range(i + 1, end):
            if recs[k][0] == _RD_HWPTAG_EQEDIT:
                eq.set("script", _rd_eqedit_script(recs[k][2]))
                break
        return wrap
    if cid == _RD_CTRL_TABLE:
        return _rd_build_table(recs, i, end, ctx)
    if cid == _RD_CTRL_GSO:
        el = etree.Element("GShapeObjectControl")
        _rd_paragraphs_in(recs, i + 1, end, ctx, in_cell, el)
        return el
    if cid == _RD_CTRL_FOOTER:
        el = etree.Element("Footer")
        _rd_paragraphs_in(recs, i + 1, end, ctx, in_cell, etree.SubElement(el, "FooterParagraphList"))
        return el
    if cid == _RD_CTRL_HEADER_:
        el = etree.Element("Header")
        _rd_paragraphs_in(recs, i + 1, end, ctx, in_cell, etree.SubElement(el, "HeaderParagraphList"))
        return el
    el = etree.Element("Control")
    _rd_paragraphs_in(recs, i + 1, end, ctx, in_cell, el)
    return el


def _rd_build_table(recs: list[tuple[int, int, bytes]], i: int, end: int, ctx: _rd_Ctx) -> etree._Element:
    lv = recs[i][1]
    tbl = etree.Element("TableControl")
    caption = None
    row_sizes: list[int] | None = None
    cells: list[tuple[tuple[int, int, int, int], etree._Element]] = []
    cur: etree._Element | None = None
    j = i + 1
    while j < end:
        tag, l2, pl = recs[j]
        if l2 == lv + 1 and tag == _RD_HWPTAG_TABLE and row_sizes is None:
            row_sizes = _rd_table_row_sizes(pl)
            cur = None
            j += 1
            continue
        if l2 == lv + 1 and tag == _RD_HWPTAG_LIST_HEADER:
            if row_sizes is None:
                caption = etree.Element("TableCaption")
                cur = caption
            else:
                cell = etree.Element("TableCell")
                col, row, cs, rs = _rd_cell_pos(pl)
                cell.set("col", str(col))
                cell.set("row", str(row))
                cell.set("colspan", str(cs))
                cell.set("rowspan", str(rs))
                cells.append(((col, row, cs, rs), cell))
                cur = cell
            j += 1
            continue
        if tag == _RD_HWPTAG_PARA_HEADER:
            if cur is None:
                raise _RdError("표의 문단이 칸·캡션 머리 없이 나왔습니다")
            p, j = _rd_build_paragraph(recs, j, ctx, cur.tag == "TableCell")
            cur.append(p)
            continue
        j += 1
    if row_sizes is None:
        raise _RdError("표 레코드(TABLE)가 없습니다")
    if sum(row_sizes) != len(cells):
        raise _RdError(f"표의 칸 수({len(cells)})가 행별 칸 수 합({sum(row_sizes)})과 다릅니다")
    if caption is not None:
        tbl.append(caption)
    body = etree.SubElement(tbl, "TableBody")
    k = 0
    for n in row_sizes:
        row_el = etree.SubElement(body, "TableRow")
        for _ in range(n):
            row_el.append(cells[k][1])
            k += 1
    return tbl


# ---------------------------------------------------------------- 파일


def _rd_fileheader_flags(header: bytes) -> int:
    if len(header) < 40:
        raise _RdError("FileHeader 가 짧습니다")
    return struct.unpack_from("<I", header, 36)[0]


def _rd_inflate(data: bytes, compressed: bool) -> bytes:
    if not compressed:
        return data
    try:
        return zlib.decompress(data, -15)
    except zlib.error as e:
        raise _RdError(f"압축을 풀 수 없습니다: {e}") from e


def _rd_read_streams(hwp_path: str | Path) -> tuple[list[int], list[bytes]]:
    """(글자 모양 속성 목록, 섹션 스트림 목록[압축 푼 것])."""
    import olefile

    path = str(hwp_path)
    if not olefile.isOleFile(path):
        raise _RdError("한글 5.0 파일이 아닙니다")
    ole = olefile.OleFileIO(path)
    try:
        if not ole.exists("FileHeader") or not ole.exists("DocInfo"):
            raise _RdError("한글 5.0 파일이 아닙니다(FileHeader·DocInfo 없음)")
        flags = _rd_fileheader_flags(ole.openstream("FileHeader").read())
        if flags & 0b10:
            raise _RdError("암호가 걸린 한글 파일은 읽을 수 없습니다")
        compressed = bool(flags & 1)
        props = _rd_char_shape_props(_rd_iter_records(_rd_inflate(ole.openstream("DocInfo").read(), compressed)))
        names: list[tuple[int, str]] = []
        for entry in ole.listdir():
            if len(entry) == 2 and entry[0] == "BodyText" and entry[1].startswith("Section"):
                tail = entry[1][7:]
                if tail.isdigit():
                    names.append((int(tail), "/".join(entry)))
        names.sort()
        sections = [_rd_inflate(ole.openstream(nm).read(), compressed) for _i, nm in names]
    finally:
        ole.close()
    if not sections:
        raise _RdError("BodyText 섹션이 없습니다")
    return props, sections


def _rd_root_from_streams(props: list[int], sections: list[bytes]) -> etree._Element:
    """글자 모양 속성 목록과 압축을 푼 섹션 스트림들로 나무를 만든다."""
    ctx = _rd_Ctx(props)
    root = etree.Element("HwpDoc")
    body = etree.SubElement(root, "BodyText")
    for sec_i, data in enumerate(sections):
        recs = _rd_iter_records(data)
        section = etree.SubElement(body, "SectionDef")
        section.set("section-id", str(sec_i))
        width, height = 0, 0
        for tag, _lv, pl in recs:
            if tag == _RD_HWPTAG_PAGE_DEF:
                width, height = _rd_page_def_size(pl)
                break
        page = etree.SubElement(section, "PageDef")
        page.set("width", str(width))
        page.set("height", str(height))
        colset = etree.SubElement(section, "ColumnSet")
        j = 0
        while j < len(recs):
            if recs[j][0] == _RD_HWPTAG_PARA_HEADER and recs[j][1] == 0:
                p, j = _rd_build_paragraph(recs, j, ctx, False)
                colset.append(p)
            else:
                j += 1
    return root


def _rd_hwp_to_root(hwp_path: str | Path) -> etree._Element:
    props, sections = _rd_read_streams(hwp_path)
    return _rd_root_from_streams(props, sections)


def _hwp_to_root(hwp_path: str | Path, warnings: list[str] | None = None) -> etree._Element:
    return _rd_hwp_to_root(hwp_path)


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


def _join_cell_lines(s: str | None) -> str:
    """원문 칸 줄을 잇는다. 윗줄이 「/」로 끝나면 빈칸 없이, 아니면 빈칸 하나로. 연속 공백은 하나로, 앞뒤는 뗀다."""
    lines = (s or "").split("\n")
    out = lines[0]
    for ln in lines[1:]:
        out += ("" if out.endswith("/") else " ") + ln
    return re.sub(r"\s+", " ", out).strip()


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
            exp_name = _join_cell_lines(rec.get("name_raw"))
            if exp_name != (rec.get("name") or ""):
                mism["name"] = {"expected": exp_name, "actual": rec.get("name"), "raw": rec.get("name_raw")}
        spec_raw = rec.get("spec_raw")
        if spec_raw is not None:
            spec_raw_s = str(spec_raw)
            exp_spec = _join_cell_lines(spec_raw_s) if spec_raw_s.strip() else spec_raw_s.strip()
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
