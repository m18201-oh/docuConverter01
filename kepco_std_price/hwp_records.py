"""한글(HWP 5.0) 원본을 olefile 로 레코드째 읽어 XML 나무를 만든다.

공개된 한글 문서 파일 형식(HWP 5.0)만 보고 쓴 독자 구현이다. 추출기(`extract_hwp`)가
걷는 나무의 모양은 다음과 같다.

    HwpDoc > BodyText > SectionDef(section-id) > PageDef(width, height)
                                               > ColumnSet > Paragraph > Text
    Paragraph 안: Text · TableControl · GShapeObjectControl · Control > EqEdit(script) · Footer …
    TableControl > [TableCaption > Paragraph] · TableBody > TableRow > TableCell(col,row,colspan,rowspan) > Paragraph

읽는 곳:
- `FileHeader` 36~39바이트 0번 비트: 본문 압축 여부(zlib raw deflate)
- `DocInfo` 의 `HWPTAG_CHAR_SHAPE`(21): 글자 모양별 속성(위 첨자·아래 첨자 비트)
- `BodyText/Section<N>` 의 레코드: PARA_HEADER(66) · PARA_TEXT(67) · PARA_CHAR_SHAPE(68)
  · PARA_LINE_SEG(69) · CTRL_HEADER(71) · LIST_HEADER(72) · PAGE_DEF(73) · TABLE(77) · EQEDIT(88)

추출기만 이 모듈을 쓴다. 보존 검사기(`conservation_hwp`)는 같은 방법을 자기 파일에 따로 둔다.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

from lxml import etree

HWPTAG_CHAR_SHAPE = 21
HWPTAG_PARA_HEADER = 66
HWPTAG_PARA_TEXT = 67
HWPTAG_PARA_CHAR_SHAPE = 68
HWPTAG_PARA_LINE_SEG = 69
HWPTAG_CTRL_HEADER = 71
HWPTAG_LIST_HEADER = 72
HWPTAG_PAGE_DEF = 73
HWPTAG_TABLE = 77
HWPTAG_EQEDIT = 88

CTRL_TABLE = b" lbt"
CTRL_GSO = b" osg"
CTRL_EQEDIT = b"deqe"
CTRL_SECD = b"dces"
CTRL_COLD = b"dloc"
CTRL_HEADER_ = b"daeh"
CTRL_FOOTER = b"toof"

# 글자 모양 속성의 비트: 15 위 첨자, 16 아래 첨자. 속성은 레코드 46바이트째 UINT32.
_CHAR_SHAPE_PROP_OFFSET = 46
_SUPERSCRIPT_BIT = 1 << 15
_SUBSCRIPT_BIT = 1 << 16

# 제어 문자(0~31). 확장 컨트롤은 CTRL_HEADER 와 짝이 되고, 인라인 컨트롤은 짝이 없지만 8글자를 차지한다.
_EXTENDED_CTRL = frozenset({1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23})
_INLINE_CTRL = frozenset({4, 5, 6, 7, 8, 9, 19, 20})
_CTRL_WIDTH = 8

_SUPERSCRIPT_MAP = {"2": "²", "3": "³"}


class HwpReadError(Exception):
    """한글 원본을 읽다가 멈추는 오류."""


# ---------------------------------------------------------------- 레코드


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
    while off < n:
        tag, level, size, payload_off = parse_record_header(data, off)
        end = payload_off + size
        if end > n:
            raise HwpReadError("레코드 본문이 잘렸습니다")
        out.append((tag, level, data[payload_off:end]))
        off = end
    return out


def char_shape_props(docinfo_records: list[tuple[int, int, bytes]]) -> list[int]:
    """DocInfo 의 글자 모양(CHAR_SHAPE) 속성을 id(0부터) 순서로."""
    props: list[int] = []
    for tag, _lv, payload in docinfo_records:
        if tag != HWPTAG_CHAR_SHAPE:
            continue
        if len(payload) < _CHAR_SHAPE_PROP_OFFSET + 4:
            raise HwpReadError("글자 모양 레코드가 짧습니다")
        props.append(struct.unpack_from("<I", payload, _CHAR_SHAPE_PROP_OFFSET)[0])
    return props


# ---------------------------------------------------------------- 문단 글


def _units(payload: bytes) -> list[int]:
    if len(payload) % 2:
        payload = payload + b"\x00"
    return list(struct.unpack("<%dH" % (len(payload) // 2), payload))


def _decode(units: list[int]) -> str:
    return struct.pack("<%dH" % len(units), *units).decode("utf-16le", errors="replace")


def _tokens(payload: bytes) -> list[tuple[str, int, int]]:
    """PARA_TEXT → [(종류, 값, 위치)]. 종류: 'ch' 글자 코드 · 'ext' 확장 컨트롤 · 'inl' 인라인 · 'ctl' 그 밖의 제어 글자.

    위치는 문단 글 안의 UTF-16 글자 위치(컨트롤은 8글자를 차지한다)다.
    """
    u = _units(payload)
    out: list[tuple[str, int, int]] = []
    i = 0
    n = len(u)
    while i < n:
        c = u[i]
        if c >= 32:
            out.append(("ch", c, i))
            i += 1
        elif c in _EXTENDED_CTRL:
            out.append(("ext", c, i))
            i += _CTRL_WIDTH
        elif c in _INLINE_CTRL:
            out.append(("inl", c, i))
            i += _CTRL_WIDTH
        else:
            out.append(("ctl", c, i))
            i += 1
    return out


def parse_para_text(payload: bytes) -> tuple[str, int]:
    """PARA_TEXT → (글, 확장 컨트롤 수). 인라인 컨트롤(탭 등)·줄바꿈·문단 끝(13)은 글에 넣지 않는다."""
    toks = _tokens(payload)
    text = _decode([v for kind, v, _p in toks if kind == "ch"])
    nctrl = sum(1 for kind, _v, _p in toks if kind == "ext")
    return text, nctrl


# 글자의 언어 갈래. 한글 문서는 글자 갈래가 바뀌는 자리에서 글 조각을 나눈다.
def _lang(code: int) -> str | None:
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


def _pairs(payload: bytes | None) -> list[tuple[int, int]]:
    """PARA_CHAR_SHAPE → [(글자 위치, 글자 모양 id)]."""
    if not payload:
        return []
    n = len(payload) // 8
    arr = struct.unpack("<%dI" % (n * 2), payload[: n * 8])
    return [(arr[2 * k], arr[2 * k + 1]) for k in range(n)]


def _line_starts(payload: bytes | None) -> list[int]:
    """PARA_LINE_SEG → 줄이 시작하는 글자 위치(줄마다 36바이트, 첫 UINT32)."""
    if not payload:
        return []
    return [struct.unpack_from("<I", payload, k * 36)[0] for k in range(len(payload) // 36)]


def _text_nodes(
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
            lg = _lang(c)
            if lg is not None:
                first = lg
                break
        cur = first or prev_lang
        for p, c, _s in run:
            lg = _lang(c)
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
        s = _decode(buf)
        prop = props[buf_shape] if 0 <= buf_shape < len(props) else 0
        if in_cell and prop & _SUBSCRIPT_BIT:
            raise HwpReadError(f"표 칸에 아래 첨자가 있습니다: {s[:40]!r}")
        if in_cell and prop & _SUPERSCRIPT_BIT:
            if any(ch not in _SUPERSCRIPT_MAP for ch in s):
                raise HwpReadError(f"표 칸에 2·3 말고 다른 위 첨자가 있습니다: {s[:40]!r}")
            s = "".join(_SUPERSCRIPT_MAP[ch] for ch in s)
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


def _subtree_end(recs: list[tuple[int, int, bytes]], i: int) -> int:
    lv = recs[i][1]
    j = i + 1
    n = len(recs)
    while j < n and recs[j][1] > lv:
        j += 1
    return j


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


def _table_row_sizes(payload: bytes) -> list[int]:
    """TABLE 레코드: 속성(4) 행 수(2) 열 수(2) 칸 간격(2) 안 여백(2×4) 다음에 행마다 칸 수."""
    if len(payload) < 18:
        raise HwpReadError("표 레코드가 짧습니다")
    nrows = struct.unpack_from("<H", payload, 4)[0]
    if len(payload) < 18 + nrows * 2:
        raise HwpReadError("표 레코드의 행 칸 수가 잘렸습니다")
    return list(struct.unpack_from("<%dH" % nrows, payload, 18))


def _cell_pos(payload: bytes) -> tuple[int, int, int, int]:
    """칸의 LIST_HEADER: 문단 수(2) 속성(4) 다음 2바이트를 건너뛰고 열·행·열 병합·행 병합(UINT16 ×4)."""
    if len(payload) < 16:
        raise HwpReadError("칸 머리가 짧습니다")
    col, row, cs, rs = struct.unpack_from("<HHHH", payload, 8)
    return col, row, cs, rs


class _Ctx:
    def __init__(self, props: list[int]) -> None:
        self.props = props


def _build_paragraph(
    recs: list[tuple[int, int, bytes]], i: int, ctx: _Ctx, in_cell: bool
) -> tuple[etree._Element, int]:
    """recs[i] 가 PARA_HEADER. (Paragraph 요소, 다음 레코드 위치)."""
    lv = recs[i][1]
    end = _subtree_end(recs, i)
    text_pl = None
    shape_pl = None
    line_pl = None
    ctrls: list[tuple[int, int]] = []
    j = i + 1
    while j < end:
        tag, l2, pl = recs[j]
        if l2 == lv + 1:
            if tag == HWPTAG_PARA_TEXT and text_pl is None:
                text_pl = pl
            elif tag == HWPTAG_PARA_CHAR_SHAPE and shape_pl is None:
                shape_pl = pl
            elif tag == HWPTAG_PARA_LINE_SEG and line_pl is None:
                line_pl = pl
            elif tag == HWPTAG_CTRL_HEADER:
                e = _subtree_end(recs, j)
                ctrls.append((j, e))
                j = e
                continue
        j += 1

    para = etree.Element("Paragraph")
    toks = _tokens(text_pl) if text_pl is not None else []
    nodes = _text_nodes(toks, _pairs(shape_pl), _line_starts(line_pl), ctx.props, in_cell)
    n_ext = sum(1 for kind, _v, _p in toks if kind == "ext")
    if n_ext != len(ctrls):
        raise HwpReadError(
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
        el = _build_control(recs, cj, ce, ctx, in_cell)
        if el is not None:
            para.append(el)
    return para, end


def _paragraphs_in(
    recs: list[tuple[int, int, bytes]], start: int, end: int, ctx: _Ctx, in_cell: bool, parent: etree._Element
) -> None:
    j = start
    while j < end:
        if recs[j][0] == HWPTAG_PARA_HEADER:
            p, j = _build_paragraph(recs, j, ctx, in_cell)
            parent.append(p)
        else:
            j += 1


def _build_control(
    recs: list[tuple[int, int, bytes]], i: int, end: int, ctx: _Ctx, in_cell: bool
) -> etree._Element | None:
    payload = recs[i][2]
    cid = payload[:4]
    if cid in (CTRL_SECD, CTRL_COLD):
        return None
    if cid == CTRL_EQEDIT:
        wrap = etree.Element("Control")
        eq = etree.SubElement(wrap, "EqEdit")
        for k in range(i + 1, end):
            if recs[k][0] == HWPTAG_EQEDIT:
                eq.set("script", _eqedit_script(recs[k][2]))
                break
        return wrap
    if cid == CTRL_TABLE:
        return _build_table(recs, i, end, ctx)
    if cid == CTRL_GSO:
        el = etree.Element("GShapeObjectControl")
        _paragraphs_in(recs, i + 1, end, ctx, in_cell, el)
        return el
    if cid == CTRL_FOOTER:
        el = etree.Element("Footer")
        _paragraphs_in(recs, i + 1, end, ctx, in_cell, etree.SubElement(el, "FooterParagraphList"))
        return el
    if cid == CTRL_HEADER_:
        el = etree.Element("Header")
        _paragraphs_in(recs, i + 1, end, ctx, in_cell, etree.SubElement(el, "HeaderParagraphList"))
        return el
    el = etree.Element("Control")
    _paragraphs_in(recs, i + 1, end, ctx, in_cell, el)
    return el


def _build_table(recs: list[tuple[int, int, bytes]], i: int, end: int, ctx: _Ctx) -> etree._Element:
    lv = recs[i][1]
    tbl = etree.Element("TableControl")
    caption = None
    row_sizes: list[int] | None = None
    cells: list[tuple[tuple[int, int, int, int], etree._Element]] = []
    cur: etree._Element | None = None
    j = i + 1
    while j < end:
        tag, l2, pl = recs[j]
        if l2 == lv + 1 and tag == HWPTAG_TABLE and row_sizes is None:
            row_sizes = _table_row_sizes(pl)
            cur = None
            j += 1
            continue
        if l2 == lv + 1 and tag == HWPTAG_LIST_HEADER:
            if row_sizes is None:
                caption = etree.Element("TableCaption")
                cur = caption
            else:
                cell = etree.Element("TableCell")
                col, row, cs, rs = _cell_pos(pl)
                cell.set("col", str(col))
                cell.set("row", str(row))
                cell.set("colspan", str(cs))
                cell.set("rowspan", str(rs))
                cells.append(((col, row, cs, rs), cell))
                cur = cell
            j += 1
            continue
        if tag == HWPTAG_PARA_HEADER:
            if cur is None:
                raise HwpReadError("표의 문단이 칸·캡션 머리 없이 나왔습니다")
            p, j = _build_paragraph(recs, j, ctx, cur.tag == "TableCell")
            cur.append(p)
            continue
        j += 1
    if row_sizes is None:
        raise HwpReadError("표 레코드(TABLE)가 없습니다")
    if sum(row_sizes) != len(cells):
        raise HwpReadError(f"표의 칸 수({len(cells)})가 행별 칸 수 합({sum(row_sizes)})과 다릅니다")
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


def _fileheader_flags(header: bytes) -> int:
    if len(header) < 40:
        raise HwpReadError("FileHeader 가 짧습니다")
    return struct.unpack_from("<I", header, 36)[0]


def _inflate(data: bytes, compressed: bool) -> bytes:
    if not compressed:
        return data
    try:
        return zlib.decompress(data, -15)
    except zlib.error as e:
        raise HwpReadError(f"압축을 풀 수 없습니다: {e}") from e


def read_streams(hwp_path: str | Path) -> tuple[list[int], list[bytes]]:
    """(글자 모양 속성 목록, 섹션 스트림 목록[압축 푼 것])."""
    import olefile

    path = str(hwp_path)
    if not olefile.isOleFile(path):
        raise HwpReadError("한글 5.0 파일이 아닙니다")
    ole = olefile.OleFileIO(path)
    try:
        if not ole.exists("FileHeader") or not ole.exists("DocInfo"):
            raise HwpReadError("한글 5.0 파일이 아닙니다(FileHeader·DocInfo 없음)")
        flags = _fileheader_flags(ole.openstream("FileHeader").read())
        if flags & 0b10:
            raise HwpReadError("암호가 걸린 한글 파일은 읽을 수 없습니다")
        compressed = bool(flags & 1)
        props = char_shape_props(iter_records(_inflate(ole.openstream("DocInfo").read(), compressed)))
        names: list[tuple[int, str]] = []
        for entry in ole.listdir():
            if len(entry) == 2 and entry[0] == "BodyText" and entry[1].startswith("Section"):
                tail = entry[1][7:]
                if tail.isdigit():
                    names.append((int(tail), "/".join(entry)))
        names.sort()
        sections = [_inflate(ole.openstream(nm).read(), compressed) for _i, nm in names]
    finally:
        ole.close()
    if not sections:
        raise HwpReadError("BodyText 섹션이 없습니다")
    return props, sections


def root_from_streams(props: list[int], sections: list[bytes]) -> etree._Element:
    """글자 모양 속성 목록과 압축을 푼 섹션 스트림들로 나무를 만든다."""
    ctx = _Ctx(props)
    root = etree.Element("HwpDoc")
    body = etree.SubElement(root, "BodyText")
    for sec_i, data in enumerate(sections):
        recs = iter_records(data)
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
        j = 0
        while j < len(recs):
            if recs[j][0] == HWPTAG_PARA_HEADER and recs[j][1] == 0:
                p, j = _build_paragraph(recs, j, ctx, False)
                colset.append(p)
            else:
                j += 1
    return root


def hwp_to_root(hwp_path: str | Path) -> etree._Element:
    props, sections = read_streams(hwp_path)
    return root_from_streams(props, sections)
