"""HWP 추출기 자체 시험 — 합성 XML (병합 셀·〃 상속·폐지·소제목·주석 경계·부표).

실행: python -m kepco_std_price.selftest_hwp
각 줄에 PASS/FAIL 을 찍고, 하나라도 FAIL 이면 종료 코드 1.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from lxml import etree

from .extract_hwp import extract_from_root
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

    return c


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = run_all()
    print(c.report())
    print("PASS" if c.all_pass() else "FAIL")
    return 0 if c.all_pass() else 1


if __name__ == "__main__":
    sys.exit(main())
