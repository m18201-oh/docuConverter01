"""HWP 추출기 자체 시험 — 합성 XML (병합 셀·〃 상속·폐지·소제목·주석 경계·부표).

실행: python -m kepco_std_price.selftest_hwp
각 줄에 PASS/FAIL 을 찍고, 하나라도 FAIL 이면 종료 코드 1.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from lxml import etree

from .extract_hwp import extract_from_root, _eq_text
from .conservation_hwp import check_conservation_root
from .render_md import render_md


def _text_para(text: str) -> str:
    return (
        f'<Paragraph><LineSeg><Text charshape-id="1" lang="ko">{text}</Text></LineSeg></Paragraph>'
    )


def _cell(col: int, row: int, text: str, colspan: int = 1, rowspan: int = 1) -> str:
    paras = "".join(
        f"<Paragraph><LineSeg><Text>{line}</Text></LineSeg></Paragraph>"
        for line in (text.split("\n") if text else [""])
    )
    return (
        f'<TableCell col="{col}" row="{row}" colspan="{colspan}" rowspan="{rowspan}" '
        f'width="1000" height="400">{paras}</TableCell>'
    )


def _row(row: int, cells: list[tuple]) -> str:
    # cells: (col, text) or (col, text, colspan)
    inner = []
    for item in cells:
        if len(item) == 2:
            col, text = item
            inner.append(_cell(col, row, text))
        else:
            col, text, cs = item
            inner.append(_cell(col, row, text, colspan=cs))
    return "<TableRow>" + "".join(inner) + "</TableRow>"


def _table(rows: list[str]) -> str:
    body = "<TableBody rows='{n}' cols='6'>".replace("{n}", str(len(rows))) + "".join(rows) + "</TableBody>"
    return (
        "<Paragraph><LineSeg><TableControl chid='tbl '>"
        + body
        + "</TableControl></LineSeg></Paragraph>"
    )


def _table_control(rows: list[str]) -> str:
    body = "<TableBody rows='{n}' cols='6'>".replace("{n}", str(len(rows))) + "".join(rows) + "</TableBody>"
    return "<TableControl chid='tbl '>" + body + "</TableControl>"


def _diagram_control() -> str:
    return (
        "<TableControl chid='tbl '>"
        "<TableBody rows='1' cols='1'>"
        "<TableRow>"
        + _cell(0, 0, "시공기준면")
        + "</TableRow></TableBody></TableControl>"
    )


def _mixed_para(*chunks: str) -> str:
    inner: list[str] = []
    for ch in chunks:
        if ch.startswith("<TableControl"):
            inner.append(ch)
        else:
            inner.append(f'<Text charshape-id="1" lang="ko">{ch}</Text>')
    return "<Paragraph><LineSeg>" + "".join(inner) + "</LineSeg></Paragraph>"


def make_xml() -> bytes:
    header = _row(
        0,
        [
            (0, "공종코드"),
            (1, "공종명칭"),
            (2, "규격"),
            (3, "단위"),
            (4, "단가"),
            (5, "노무비율"),
        ],
    )
    rec1 = _row(
        1,
        [
            (0, "AD160.10000"),
            (1, "전력구 청소"),
            (2, "-"),
            (3, "㎡"),
            (4, "4,403"),
            (5, "100.00%"),
        ],
    )
    rec_ab = _row(
        1,
        [
            (0, "AE230.30000"),
            (1, "주형브레이싱 철거"),
            (2, "-"),
            (3, "set"),
            (4, "폐지"),
            (5, "‘26상"),
        ],
    )
    # 소제목: 코드 + 병합 명칭 (오른쪽 칸을 채우지 않음)
    sub = _row(
        1,
        [
            (0, "ND109.261*"),
            (1, "메사쉴드 갱내 자재 소운반/철근, 강재, 시멘트류", 5),
        ],
    )
    inh = _row(
        2,
        [
            (0, "ND109.26111"),
            (1, "“"),
            (2, "L=50m"),
            (3, "ton"),
            (4, "1,000"),
            (5, "50.00%"),
        ],
    )
    header2 = header  # 공종명칭 헤더 재사용
    # 공종명 헤더
    header_alt = _row(
        0,
        [
            (0, "공종코드"),
            (1, "공종명"),
            (2, "규격"),
            (3, "단위"),
            (4, "단가"),
            (5, "노무비율"),
        ],
    )
    rec_alt = _row(
        1,
        [
            (0, "AA240.11000"),
            (1, "지장물보호"),
            (2, "D600㎜ 미만"),
            (3, "nr"),
            (4, "130,131"),
            (5, "0%"),
        ],
    )
    banner = (
        "<Paragraph><LineSeg><TableControl chid='tbl '>"
        "<TableBody rows='1' cols='2'>"
        "<TableRow>"
        + _cell(0, 0, "대분류 A")
        + _cell(1, 0, "공통공사")
        + "</TableRow></TableBody></TableControl></LineSeg></Paragraph>"
    )
    subtable = (
        "<Paragraph><LineSeg><TableControl chid='tbl '>"
        "<TableBody rows='2' cols='3'>"
        "<TableRow>"
        + _cell(0, 0, "구분")
        + _cell(1, 0, "전용횟수")
        + _cell(2, 0, "적용기준")
        + "</TableRow>"
        "<TableRow>"
        + _cell(0, 1, "보통마감")
        + _cell(1, 1, "4회")
        + _cell(2, 1, "측구, 수로")
        + "</TableRow></TableBody></TableControl></LineSeg></Paragraph>"
    )
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<HwpDoc version="5.1.1.0">
  <BodyText>
    <SectionDef section-id="0">
      <PageDef width="59528" height="84188" orientation="portrait"/>
      <ColumnSet>
        {_text_para("토목분야 자체 표준시장단가")}
        {banner}
        {_text_para("■ AD16** 현장정리")}
        {_table([header, rec1])}
        {_text_para("【단가정의】")}
        {_text_para("① 이 단가는 전력구공사에 적용한다.")}
        {_text_para("② 공사 중 청소 비용을 포함한다.")}
        {_text_para("․ 뒷정리 포함")}
        {_text_para("- 준공 시 청소")}
        {subtable}
        {_text_para("■ AE23* 주형보")}
        {_table([header, rec_ab])}
        {_text_para("【단가정의】")}
        {_text_para("① 폐지된 공종이다.")}
        {_text_para("■ ND10* 본선 시설공")}
        {_table([header, sub, inh])}
        {_text_para("【단가정의】")}
        {_text_para("※ 소제목 아래 상속 행.")}
        {_text_para("■ AA24* 지장물보호")}
        {_table([header_alt, rec_alt])}
        {_text_para("【단가정의】")}
        {_text_para("① 설치 및 철거 비용이다.")}
      </ColumnSet>
    </SectionDef>
  </BodyText>
</HwpDoc>
"""
    return xml.encode("utf-8")


def _ts_header() -> str:
    return _row(
        0,
        [
            (0, "공종코드"),
            (1, "공종명칭"),
            (2, "규격"),
            (3, "단위"),
            (4, "단가"),
            (5, "노무비율"),
        ],
    )


def _ts_rec(row: int, code: str, name: str) -> str:
    return _row(
        row,
        [
            (0, code),
            (1, name),
            (2, "-"),
            (3, "m"),
            (4, "1,000"),
            (5, "10.00%"),
        ],
    )


def make_xml_table_seq(body: str) -> bytes:
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<HwpDoc version="5.1.1.0">
  <BodyText>
    <SectionDef section-id="0">
      <PageDef width="59528" height="84188" orientation="portrait"/>
      <ColumnSet>
        {_text_para("토목분야 자체 표준시장단가")}
        {body}
      </ColumnSet>
    </SectionDef>
  </BodyText>
</HwpDoc>
"""
    return xml.encode("utf-8")


def make_xml_table_title(body: str) -> bytes:
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<HwpDoc version="5.1.1.0">
  <BodyText>
    <SectionDef section-id="0">
      <PageDef width="59528" height="84188" orientation="portrait"/>
      <ColumnSet>
        {_text_para("토목분야 자체 표준시장단가")}
        {body}
      </ColumnSet>
    </SectionDef>
  </BodyText>
</HwpDoc>
"""
    return xml.encode("utf-8")


class Check:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        self.results.append((name, cond, detail))

    def all_pass(self) -> bool:
        return all(ok for _, ok, _ in self.results)

    def report(self) -> str:
        lines = []
        for name, ok, detail in self.results:
            status = "PASS" if ok else "FAIL"
            suffix = f" - {detail}" if (detail and not ok) else ""
            lines.append(f"{status} {name}{suffix}")
        return "\n".join(lines)


def run_all() -> Check:
    c = Check()
    root = etree.fromstring(make_xml())
    result = extract_from_root(root, "2026H1", sha="deadbeef")
    recs = {r["code"]: r for r in result["records"]}
    groups = result["groups"]
    subs = result["subheaders"]

    c.check("H1 records==4", len(result["records"]) == 4, f"n={len(result['records'])}")
    c.check("H2 groups==4", len(groups) == 4, f"n={len(groups)}")
    c.check("H3 pages layout hwp", result["pages"] and result["pages"][0]["layout"] == "hwp")
    c.check(
        "H4 PageDef size",
        result["pages"][0]["width"] == 59528 and result["pages"][0]["height"] == 84188,
        str(result["pages"][0]),
    )

    r = recs.get("AD160.10000")
    c.check("H5 present price", bool(r) and r["price"] == 4403 and r["status"] == "present", str(r))
    c.check("H6 pdf_page null", bool(r) and r["pdf_page"] is None and r["bbox"] is None)
    c.check("H7 hwp_anchor", bool(r) and isinstance(r.get("hwp_anchor"), dict) and r["hwp_anchor"]["row"] == 1)

    ab = recs.get("AE230.30000")
    c.check(
        "H8 abolished",
        bool(ab) and ab["status"] == "abolished" and ab["price"] is None and ab.get("abolished_at"),
        str(ab),
    )

    inh = recs.get("ND109.26111")
    c.check(
        "H9 inherit from subheader",
        bool(inh)
        and inh["name_inherited"] is True
        and "메사쉴드" in (inh.get("name") or "")
        and inh.get("name_group") == "ND109.261*",
        str(inh),
    )
    c.check("H10 subheader emitted", any(s.get("code_pattern") == "ND109.261*" for s in subs), str(subs))
    c.check(
        "H11 merged not filled right",
        bool(inh) and (inh.get("spec") or "").startswith("L="),
        str(inh),
    )

    alt = recs.get("AA240.11000")
    c.check("H12 공종명 header mapped", bool(alt) and alt["name"] == "지장물보호", str(alt))
    c.check("H13 labor 0%", bool(alt) and alt["labor_ratio"] == 0.0, str(alt))

    g0 = groups[0]
    notes = g0.get("notes") or []
    items = [n.get("item") or "" for n in notes]
    c.check("H14 notes has ①", any(x.startswith("①") for x in items), str(items))
    c.check(
        "H15 ․ and - attached to ②",
        any("뒷정리" in x and "준공" in x for x in items),
        str(items),
    )
    c.check(
        "H16 subtable (표)",
        any(n.get("subtable") or str(n.get("item") or "").startswith("(표)") for n in notes),
        str(items),
    )
    c.check("H17 major from banner", g0.get("major") == "A" and g0.get("major_name") == "공통공사", str(g0))
    c.check("H18 field 토목", g0.get("field") == "토목" and (r or {}).get("field") == "토목")

    report = check_conservation_root(root, result)
    tot = report.get("totals") or {}
    c.check("H19 conservation pass", tot.get("pass") is True, str(tot))
    c.check("H20 missing 0", tot.get("missing") == 0, str(tot))
    c.check("H21 empty_name 0", tot.get("empty_name") == 0, str(tot))
    c.check("H22 unresolved_inherit 0", tot.get("unresolved_inherit") == 0, str(tot))
    c.check("H23 parse_mismatch 0", tot.get("parse_mismatch") == 0, str(tot.get("parse_mismatch")))
    c.check("H24 inherit_mismatch 0", tot.get("inherit_mismatch") == 0, str(tot))

    with tempfile.TemporaryDirectory(prefix="g03_hwp_md_") as tmp:
        md_dir = Path(tmp) / "md"
        summary = render_md(result, md_dir, "synth.hwp")
        files = list(md_dir.rglob("*.md"))
        c.check("H25 md written", len(files) >= 1, str([p.name for p in files]))
        text = files[0].read_text(encoding="utf-8") if files else ""
        c.check("H26 md 원문 표N행M", "표" in text and "행" in text and "](<" not in text.split("원문")[-1][:200], text[:400])
        c.check("H27 md layout hwp", "layout: hwp" in text)
        c.check("H28 md no crash", summary is not None)

    from .spec import BANNER_RE, HALF_LABOR_RE

    c.check("L1 U+2019 ’25상", bool(HALF_LABOR_RE.match("\u201925상")))
    c.check("L1 U+FF07 ＇25상", bool(HALF_LABOR_RE.match("\uff0725상")))
    c.check("L1 U+0027 '25상", bool(HALF_LABOR_RE.match("'25상")))

    m3 = BANNER_RE.search("대분류 Q, R, S 기타공사")
    c.check(
        "L6 banner 3 letters",
        bool(m3) and m3.group(1) == "Q" and "기타공사" in (m3.group(2) or ""),
        str(m3.groups() if m3 else None),
    )

    rec_u2019 = _row(1, [(0, "AE230.30001"), (1, "철거"), (2, "-"), (3, "set"), (4, "폐기"), (5, "\u201925상")])
    rec_uff07 = _row(1, [(0, "AE230.30002"), (1, "철거"), (2, "-"), (3, "set"), (4, "폐기"), (5, "\uff0725상")])
    rec_u0027 = _row(1, [(0, "AE230.30003"), (1, "철거"), (2, "-"), (3, "set"), (4, "폐기"), (5, "'25상")])
    rec_dec = _row(1, [(0, "AE230.30004"), (1, "소수단가"), (2, "-"), (3, "m"), (4, "1,234.5"), (5, "10.00%")])
    header_lat = _row(
        0,
        [(0, "공종코드"), (1, "공종명칭"), (2, "규격"), (3, "단위"), (4, "단가"), (5, "노무비율")],
    )
    banner_qrs = (
        "<Paragraph><LineSeg><TableControl chid='tbl '>"
        "<TableBody rows='1' cols='2'><TableRow>"
        + _cell(0, 0, "대분류 Q, R, S")
        + _cell(1, 0, "기타공사")
        + "</TableRow></TableBody></TableControl></LineSeg></Paragraph>"
    )
    diagram = (
        "<Paragraph><LineSeg><TableControl chid='tbl '>"
        "<TableBody rows='2' cols='3'>"
        "<TableRow>"
        + _cell(0, 0, "ND109.11111 0-150m이내, 양호")
        + _cell(1, 0, "ND109.11112 0-150m이내, 보통")
        + _cell(2, 0, "ND109.11113 0-150m이내, 불량")
        + "</TableRow>"
        "<TableRow>"
        + _cell(0, 1, "품셈으로 산출")
        + _cell(1, 1, "")
        + _cell(2, 1, "")
        + "</TableRow></TableBody></TableControl></LineSeg></Paragraph>"
    )
    xml_lat = f"""<?xml version="1.0" encoding="utf-8"?>
<HwpDoc version="5.1.1.0">
  <BodyText>
    <SectionDef section-id="0">
      <PageDef width="59528" height="84188" orientation="portrait"/>
      <ColumnSet>
        {_text_para("토목분야 자체 표준시장단가")}
        {banner_qrs}
        {_text_para("■ AE23* 주형보")}
        {_table([header_lat, rec_u2019])}
        {_table([header_lat, rec_uff07])}
        {_table([header_lat, rec_u0027])}
        {_table([header_lat, rec_dec])}
        {_text_para("【단가정의】")}
        {_text_para("① 본 단가 적용을 위한 수량산출 예는 아래와 같다.")}
        {_text_para("굴진방향→")}
        {diagram}
      </ColumnSet>
    </SectionDef>
  </BodyText>
</HwpDoc>
"""
    root_lat = etree.fromstring(xml_lat.encode("utf-8"))
    res_lat = extract_from_root(root_lat, "2026H1", sha="beef")
    recs_lat = {r["code"]: r for r in res_lat["records"]}
    for code, mark in (
        ("AE230.30001", "U+2019"),
        ("AE230.30002", "U+FF07"),
        ("AE230.30003", "U+0027"),
    ):
        rlat = recs_lat.get(code)
        c.check(
            f"L1 extract abolished {mark}",
            bool(rlat) and rlat.get("status") == "abolished" and rlat.get("price") is None,
            str(rlat),
        )
    rdec = recs_lat.get("AE230.30004")
    c.check(
        "L2 unparsed price kept",
        bool(rdec) and rdec.get("price") is None and "1,234.5" in str(rdec.get("price_raw") or ""),
        str(rdec),
    )
    g_lat = res_lat["groups"][0] if res_lat["groups"] else {}
    c.check(
        "L6 major Q 기타공사",
        g_lat.get("major") == "Q" and g_lat.get("major_name") == "기타공사",
        str(g_lat),
    )
    notes_lat = [n.get("item") or "" for n in (g_lat.get("notes") or [])]
    c.check(
        "H1 notes end at 수량산출 예",
        bool(notes_lat) and "수량산출 예" in notes_lat[-1] and not any(x.startswith("(표)") for x in notes_lat),
        str(notes_lat),
    )

    def _g_seqs(result: dict, gidx: int = 0) -> tuple[list, list]:
        g = result["groups"][gidx]
        gid = g["group_id"]
        recs = [r["table_seq"] for r in result["records"] if r["group_id"] == gid]
        notes = [n.get("table_seq") for n in (g.get("notes") or [])]
        return recs, notes

    hdr = _ts_header()
    body_ga_da = (
        f"{_text_para('■ TS01* 가')}"
        f"{_table([hdr, _ts_rec(1, 'TS010.10000', '가1')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('① 이 단가는 가1에 적용한다.')}"
        f"{_text_para('② 이 단가는 가2에 적용한다.')}"
        f"{_table([hdr, _ts_rec(1, 'TS010.20000', '가2')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('① 이 단가는 가3에 적용한다.')}"
        f"{_text_para('■ TS02* 다')}"
        f"{_table([hdr, _ts_rec(1, 'TS020.10000', '다1')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('① 이 단가는 다에 적용한다.')}"
    )
    res_ga = extract_from_root(etree.fromstring(make_xml_table_seq(body_ga_da)), "2026H1", sha="ts")
    rec_ga, note_ga = _g_seqs(res_ga, 0)
    rec_da, note_da = _g_seqs(res_ga, 1)
    c.check("TS가 레코드 1,2 / 주석 1,1,2", rec_ga == [1, 2] and note_ga == [1, 1, 2], str((rec_ga, note_ga)))
    c.check("TS다 새 그룹은 다시 1", rec_da == [1] and note_da == [1], str((rec_da, note_da)))

    body_na = (
        f"{_text_para('■ TS03* 나')}"
        f"{_table([hdr, _ts_rec(1, 'TS030.10000', '나1')])}"
        f"{_table([hdr, _ts_rec(1, 'TS030.20000', '나2')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('① 이 단가는 나1에 적용한다.')}"
        f"{_text_para('② 이 단가는 나2에 적용한다.')}"
    )
    res_na = extract_from_root(etree.fromstring(make_xml_table_seq(body_na)), "2026H1", sha="ts")
    rec_na, note_na = _g_seqs(res_na, 0)
    c.check("TS나 레코드·주석 전부 1", rec_na == [1, 1] and note_na == [1, 1], str((rec_na, note_na)))

    body_ra = (
        f"{_text_para('■ TS04* 라')}"
        f"{_table([hdr, _ts_rec(1, 'TS040.10000', '라1')])}"
        f"{_text_para('① 이 단가는 라에 적용한다.')}"
        f"{_table([hdr, _ts_rec(1, 'TS040.20000', '라2')])}"
    )
    res_ra = extract_from_root(etree.fromstring(make_xml_table_seq(body_ra)), "2026H1", sha="ts")
    rec_ra, note_ra = _g_seqs(res_ra, 0)
    c.check("TS라 라벨 없이 ① → 레코드 1,2 / 주석 1", rec_ra == [1, 2] and note_ra == [1], str((rec_ra, note_ra)))

    body_ma = (
        f"{_text_para('■ TS05* 마')}"
        f"{_table([hdr, _ts_rec(1, 'TS050.10000', '마1')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('① 이 단가는 마1에 적용한다.')}"
        f"{_table([hdr, _ts_rec(1, 'TS050.20000', '마2')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('※ 이 단가는 마2에 적용한다.')}"
    )
    res_ma = extract_from_root(etree.fromstring(make_xml_table_seq(body_ma)), "2026H1", sha="ts")
    rec_ma, note_ma = _g_seqs(res_ma, 0)
    c.check("TS마 ※ 주석 → 레코드 1,2 / 주석 1,2", rec_ma == [1, 2] and note_ma == [1, 2], str((rec_ma, note_ma)))

    body_ba = (
        f"{_text_para('■ TS06* 바')}"
        f"{_table([hdr, _ts_rec(1, 'TS060.10000', '바1')])}"
        f"{_table([hdr, _ts_rec(1, 'TS060.20000', '바2')])}"
    )
    res_ba = extract_from_root(etree.fromstring(make_xml_table_seq(body_ba)), "2026H1", sha="ts")
    rec_ba, note_ba = _g_seqs(res_ba, 0)
    c.check("TS바 주석 없음 → 레코드 전부 1", rec_ba == [1, 1] and note_ba == [], str((rec_ba, note_ba)))

    body_sa = (
        f"{_text_para('■ TS07* 사')}"
        f"{_table([hdr, _ts_rec(1, 'TS070.10000', '사1')])}"
        f"{_text_para('【단가정의】')}"
        f"{_table([hdr, _ts_rec(1, 'TS070.20000', '사2')])}"
    )
    res_sa = extract_from_root(etree.fromstring(make_xml_table_seq(body_sa)), "2026H1", sha="ts")
    rec_sa, note_sa = _g_seqs(res_sa, 0)
    c.check("TS사 라벨만 → 레코드 1,2 / 주석 없음", rec_sa == [1, 2] and note_sa == [], str((rec_sa, note_sa)))

    body_ga1 = (
        f"{_text_para('■ NA20* 수직구 / 토사굴착')}"
        f"{_table([hdr, _ts_rec(1, 'NA200.10000', '토사굴착')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('① 이 단가는 수직구 토사굴착에 적용한다.')}"
        f"{_text_para('② 이 단가는 수직구 토사굴착에 적용한다.')}"
        f"{_mixed_para('③ 본 단가의 적용을 위한 수량 산출은 아래와 같다.', _diagram_control())}"
    )
    res_ga1 = extract_from_root(etree.fromstring(make_xml_table_seq(body_ga1)), "2026H1", sha="g08a")
    notes_ga1 = [n.get("item") or "" for n in (res_ga1["groups"][0].get("notes") or [])]
    c.check(
        "가1 표와 같은 문단의 ③이 빠지지 않는다",
        len(notes_ga1) == 3 and notes_ga1[2].startswith("③"),
        str(notes_ga1),
    )

    body_ga2 = (
        f"{_text_para('■ DK32* 소나무 식재')}"
        f"{_mixed_para(_table_control([hdr, _ts_rec(1, 'DK320.10000', '소나무1')]), '【단가정의】')}"
        f"{_text_para('① 이 단가는 소나무 식재에 적용한다.')}"
        f"{_table([hdr, _ts_rec(1, 'DK320.20000', '소나무2')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('① 이 단가는 소나무 식재에 적용한다.')}"
    )
    res_ga2 = extract_from_root(etree.fromstring(make_xml_table_seq(body_ga2)), "2026H1", sha="g08a")
    rec_ga2, note_ga2 = _g_seqs(res_ga2, 0)
    c.check(
        "가2 표 뒤 【단가정의】 → 레코드 1,2 / 주석 1,2",
        rec_ga2 == [1, 2] and note_ga2 == [1, 2],
        str((rec_ga2, note_ga2)),
    )

    body_ga3 = (
        f"{_text_para('■ XX10* 글이 표보다 앞')}"
        f"{_mixed_para('① 이 단가는 글이 표보다 앞에 있을 때 적용한다.', _table_control([hdr, _ts_rec(1, 'XX100.10000', '앞글')]))}"
    )
    res_ga3 = extract_from_root(etree.fromstring(make_xml_table_seq(body_ga3)), "2026H1", sha="g08a")
    rec_ga3, note_ga3 = _g_seqs(res_ga3, 0)
    notes_ga3 = [n.get("item") or "" for n in (res_ga3["groups"][0].get("notes") or [])]
    c.check(
        "가3 글이 표보다 앞 → 주석 ①, 레코드 table_seq 1",
        rec_ga3 == [1] and note_ga3 == [1] and bool(notes_ga3) and notes_ga3[0].startswith("①"),
        str((rec_ga3, note_ga3, notes_ga3)),
    )

    body_na1 = (
        f"{_text_para('■ MA***** 타일공사')}"
        f"{_table([hdr, _ts_rec(1, 'MA000.10000', '타일')])}"
        f"{_text_para('【단가정의】')}"
        "<Paragraph><LineSeg>"
        '<Text charshape-id="1" lang="ko">① 이 단가는 500V표면저항치 </Text>'
        '<EqEdit script="2.5` TIMES 10  ^{4`}"/>'
        '<Text charshape-id="1" lang="ko">∼Ω 에 적용한다.</Text>'
        "</LineSeg></Paragraph>"
    )
    res_na1 = extract_from_root(etree.fromstring(make_xml_table_seq(body_na1)), "2026H1", sha="g08b")
    notes_na1 = [n.get("item") or "" for n in (res_na1["groups"][0].get("notes") or [])]
    c.check(
        "나1 주석 속 수식이 2.5×10⁴ 로 들어간다",
        bool(notes_na1) and "2.5×10⁴" in notes_na1[0] and "TIMES" not in notes_na1[0],
        str(notes_na1),
    )

    c.check("나2 _eq_text 1.0×10⁶", _eq_text("1.0 TIMES 10  ^{6`}") == "1.0×10⁶", _eq_text("1.0 TIMES 10  ^{6`}"))
    c.check("나2 _eq_text a over b 유지", _eq_text("a over b") == "a over b")
    c.check("나2 _eq_text x^2", _eq_text("x^2") == "x²")

    body_na3 = (
        f"{_text_para('■ EQ00* 빈수식')}"
        f"{_table([hdr, _ts_rec(1, 'EQ000.10000', '빈수식')])}"
        f"{_text_para('【단가정의】')}"
        "<Paragraph><LineSeg>"
        '<Text charshape-id="1" lang="ko">① 이 단가는 </Text>'
        "<EqEdit/>"
        '<Text charshape-id="1" lang="ko">빈수식에 적용한다.</Text>'
        "</LineSeg></Paragraph>"
    )
    res_na3 = extract_from_root(etree.fromstring(make_xml_table_seq(body_na3)), "2026H1", sha="g08b")
    notes_na3 = [n.get("item") or "" for n in (res_na3["groups"][0].get("notes") or [])]
    c.check(
        "나3 script 없는 EqEdit 은 끼우지 않는다",
        bool(notes_na3) and "① 이 단가는 빈수식에 적용한다." in notes_na3[0],
        str(notes_na3),
    )

    hdr = _ts_header()
    body_tt1 = (
        f"{_text_para('■ TT01* 제목')}"
        f"{_text_para('- 가 구간')}"
        f"{_table([hdr, _ts_rec(1, 'TT010.10000', '가1'), _ts_rec(2, 'TT010.10001', '가2')])}"
        f"{_text_para('- 나 구간')}"
        f"{_table([hdr, _ts_rec(1, 'TT010.20000', '나1')])}"
        f"{_table([hdr, _ts_rec(1, 'TT010.20001', '나2')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('① 이 단가는 가·나에 적용한다.')}"
    )
    res_tt1 = extract_from_root(etree.fromstring(make_xml_table_title(body_tt1)), "2026H1", sha="tt")
    recs_tt1 = [r for r in res_tt1["records"] if r["group_id"] == res_tt1["groups"][0]["group_id"]]
    titles_tt1 = [r.get("table_title") for r in recs_tt1]
    c.check(
        "제1 첫 표 - 가 구간, 둘째·셋째 표 - 나 구간",
        titles_tt1 == ["- 가 구간", "- 가 구간", "- 나 구간", "- 나 구간"],
        str(titles_tt1),
    )

    body_tt2 = (
        f"{_text_para('■ TT02* 제목없음')}"
        f"{_table([hdr, _ts_rec(1, 'TT020.10000', '가')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('① 이 단가는 가에 적용한다.')}"
        f"{_table([hdr, _ts_rec(1, 'TT020.20000', '나')])}"
    )
    res_tt2 = extract_from_root(etree.fromstring(make_xml_table_title(body_tt2)), "2026H1", sha="tt")
    titles_tt2 = [r.get("table_title") for r in res_tt2["records"]]
    c.check("제2 주석 앞뒤 표 전부 null", titles_tt2 == [None, None], str(titles_tt2))

    body_tt3 = (
        f"{_text_para('■ TT03* 주석뒤')}"
        f"{_text_para('- 가 구간')}"
        f"{_table([hdr, _ts_rec(1, 'TT030.10000', '가')])}"
        f"{_text_para('【단가정의】')}"
        f"{_text_para('① 이 단가는 가에 적용한다.')}"
        f"{_table([hdr, _ts_rec(1, 'TT030.20000', '나')])}"
    )
    res_tt3 = extract_from_root(etree.fromstring(make_xml_table_title(body_tt3)), "2026H1", sha="tt")
    titles_tt3 = [r.get("table_title") for r in res_tt3["records"]]
    c.check(
        "제3 첫 표 - 가 구간, 둘째 표 null",
        titles_tt3 == ["- 가 구간", None],
        str(titles_tt3),
    )

    body_tt4 = (
        f"{_text_para('■ TT04* 바로위')}"
        f"{_text_para('- 가 구간')}"
        f"{_text_para('- 나 구간')}"
        f"{_table([hdr, _ts_rec(1, 'TT040.10000', '나')])}"
    )
    res_tt4 = extract_from_root(etree.fromstring(make_xml_table_title(body_tt4)), "2026H1", sha="tt")
    titles_tt4 = [r.get("table_title") for r in res_tt4["records"]]
    c.check("제4 표 바로 위 한 줄만", titles_tt4 == ["- 나 구간"], str(titles_tt4))

    body_tt5 = (
        f"{_text_para('■ TT05* 가')}"
        f"{_text_para('- 가 구간')}"
        f"{_table([hdr, _ts_rec(1, 'TT050.10000', '가')])}"
        f"{_text_para('■ TT06* 나')}"
        f"{_table([hdr, _ts_rec(1, 'TT060.10000', '나')])}"
    )
    res_tt5 = extract_from_root(etree.fromstring(make_xml_table_title(body_tt5)), "2026H1", sha="tt")
    titles_tt5 = [(r["code"], r.get("table_title")) for r in res_tt5["records"]]
    c.check(
        "제5 새 그룹의 표는 null",
        titles_tt5 == [("TT050.10000", "- 가 구간"), ("TT060.10000", None)],
        str(titles_tt5),
    )

    keys_tt1 = [list(r.keys()) for r in recs_tt1]
    key_ok = all(
        "table_seq" in ks and ks[ks.index("table_seq") + 1] == "table_title" for ks in keys_tt1
    )
    c.check("제6 table_title 키가 table_seq 바로 뒤에 있다", key_ok, str(keys_tt1[0] if keys_tt1 else None))

    from .extract_hwp import _join_cell_lines as _hwp_join_cell_lines

    c.check(
        "K1 S1 / 줄바꿈은 빈칸 없이",
        _hwp_join_cell_lines("쉬트파일박기/진동식/\n(N≦15)") == "쉬트파일박기/진동식/(N≦15)",
    )
    c.check(
        "K1 S2 / 뒤 빈칸은 유지",
        _hwp_join_cell_lines("쉬트파일박기/진동식/ \n(N≦15)") == "쉬트파일박기/진동식/ (N≦15)",
    )
    c.check("K1 S3 일반 줄바꿈은 빈칸", _hwp_join_cell_lines("콘크리트\n타설") == "콘크리트 타설")
    c.check("K1 S4 연속 / 줄바꿈", _hwp_join_cell_lines("가/\n나/\n다") == "가/나/다")

    # ---- 한글 원본 레코드 직접 읽기 (G10 A): 만든 바이트열로 시험한다 ----
    import struct

    from . import conservation_hwp as _gate
    from . import hwp_records as hr

    def rec(tag: int, level: int, payload: bytes) -> bytes:
        size = len(payload)
        if size >= 0xFFF:
            return struct.pack("<II", tag | (level << 10) | (0xFFF << 20), size) + payload
        return struct.pack("<I", tag | (level << 10) | (size << 20)) + payload

    def u16(s: str) -> bytes:
        return s.encode("utf-16le")

    SUP = 1 << 15
    SUB = 1 << 16

    # K2: 위 첨자 글자 모양 → 표 칸 안의 2·3 은 ²·³, 표 밖은 그대로
    def para(level: int, text: str, shapes: list[tuple[int, int]]) -> bytes:
        return (
            rec(hr.HWPTAG_PARA_HEADER, level, b"\x00" * 24)
            + rec(hr.HWPTAG_PARA_TEXT, level + 1, u16(text + "\r"))
            + rec(
                hr.HWPTAG_PARA_CHAR_SHAPE,
                level + 1,
                b"".join(struct.pack("<II", pos, sid) for pos, sid in shapes),
            )
            + rec(hr.HWPTAG_PARA_LINE_SEG, level + 1, struct.pack("<I", 0) + b"\x00" * 32)
        )

    def table_para(cell_texts: list[tuple[str, list[tuple[int, int]]]]) -> bytes:
        # 최상위 문단 하나 + 표 컨트롤(행 하나, 칸 여럿)
        n = len(cell_texts)
        out = rec(hr.HWPTAG_PARA_HEADER, 0, b"\x00" * 24)
        out += rec(hr.HWPTAG_PARA_TEXT, 1, u16(chr(11) + "\x00" * 7 + "\r"))
        out += rec(hr.HWPTAG_PARA_CHAR_SHAPE, 1, struct.pack("<II", 0, 0))
        out += rec(hr.HWPTAG_PARA_LINE_SEG, 1, struct.pack("<I", 0) + b"\x00" * 32)
        out += rec(hr.HWPTAG_CTRL_HEADER, 1, b" lbt" + b"\x00" * 42)
        out += rec(
            hr.HWPTAG_TABLE,
            2,
            struct.pack("<IHHH", 0, 1, n, 0) + b"\x00" * 8 + struct.pack("<H", n) + struct.pack("<H", 0),
        )
        for col, (text, shapes) in enumerate(cell_texts):
            out += rec(
                hr.HWPTAG_LIST_HEADER,
                2,
                struct.pack("<HIH", 1, 0, 0) + struct.pack("<HHHH", col, 0, 1, 1) + b"\x00" * 14,
            )
            out += para(2, text, shapes)
        return out

    top = para(0, "m2", [(0, 0), (1, 1)])
    tbl = table_para([("m2", [(0, 0), (1, 1)]), ("m3 4", [(0, 0), (1, 1), (2, 0)])])
    # 표 칸 안: "m2" 의 2 만 위 첨자, "m3 4" 의 3 만 위 첨자
    root = hr.root_from_streams([0, SUP], [top + tbl])
    nodes_top = [t.text for t in root.find("BodyText/SectionDef/ColumnSet")[0].iter("Text")]
    cells = root.findall(".//TableCell")
    cell_txt = ["".join(x.itertext()) for x in cells]
    c.check("K2 표 밖 위 첨자는 그대로", nodes_top == ["m", "2"], repr(nodes_top))
    c.check("K2 표 칸 위 첨자 2·3 은 ²·³", cell_txt == ["m²", "m³ 4"], repr(cell_txt))
    c.check(
        "K2 칸 위치(열·행·병합)를 읽는다",
        [(x.get("col"), x.get("row"), x.get("colspan"), x.get("rowspan")) for x in cells]
        == [("0", "0", "1", "1"), ("1", "0", "1", "1")],
    )
    gate_root = _gate._rd_root_from_streams([0, SUP], [top + tbl])
    c.check(
        "K2 검사기 쪽 읽기 사본도 같은 나무",
        etree.tostring(gate_root) == etree.tostring(root),
    )

    def read_err(props: list[int], data: bytes) -> bool:
        try:
            hr.root_from_streams(props, [data])
        except hr.HwpReadError:
            return True
        return False

    # K3: 표 칸 안의 아래 첨자·2·3 말고 다른 위 첨자는 멈춘다(표 밖은 상관없다)
    c.check(
        "K3 표 칸 아래 첨자는 오류",
        read_err([0, SUB], table_para([("x1", [(0, 0), (1, 1)])])),
    )
    c.check(
        "K3 표 칸 2·3 아닌 위 첨자는 오류",
        read_err([0, SUP], table_para([("m4", [(0, 0), (1, 1)])])),
    )
    c.check(
        "K3 표 밖 아래 첨자는 오류 아님",
        not read_err([0, SUB], para(0, "x1", [(0, 0), (1, 1)])),
    )

    h = hr.HWPTAG_PARA_HEADER | (1 << 10) | (0xFFF << 20)
    rec_bytes = h.to_bytes(4, "little") + (4096).to_bytes(4, "little") + b"\x00" * 8
    tag, level, size, payload_off = hr.parse_record_header(rec_bytes, 0)
    c.check(
        "K4 레코드 머리 0xFFF",
        (tag, level, size, payload_off) == (66, 1, 4096, 8),
        repr((tag, level, size, payload_off)),
    )
    big = rec(hr.HWPTAG_PARA_TEXT, 1, b"\x41\x00" * 3000)
    recs_big = hr.iter_records(big + rec(hr.HWPTAG_PARA_HEADER, 0, b"\x00" * 24))
    c.check(
        "K4 큰 레코드 뒤 레코드도 읽는다",
        [(t, lv, len(p)) for t, lv, p in recs_big] == [(67, 1, 6000), (66, 0, 24)],
    )

    body = "가" + chr(11) + "\0" * 7 + "나" + chr(9) + "\0" * 7 + "다" + chr(10) + chr(13)
    text, nctrl = hr.parse_para_text(body.encode("utf-16le"))
    c.check("K5 PARA_TEXT 풀기", (text, nctrl) == ("가나다", 1), repr((text, nctrl)))

    # K6: 확장 컨트롤 수와 CTRL_HEADER 수가 다르면 멈춘다
    broken = (
        rec(hr.HWPTAG_PARA_HEADER, 0, b"\x00" * 24)
        + rec(hr.HWPTAG_PARA_TEXT, 1, u16(chr(11) + "\x00" * 7 + "\r"))
        + rec(hr.HWPTAG_PARA_CHAR_SHAPE, 1, struct.pack("<II", 0, 0))
    )
    c.check("K6 컨트롤 수가 안 맞으면 오류", read_err([0], broken))

    return c


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = run_all()
    print(c.report())
    print("PASS" if c.all_pass() else "FAIL")
    return 0 if c.all_pass() else 1


if __name__ == "__main__":
    sys.exit(main())
