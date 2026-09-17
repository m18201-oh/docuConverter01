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
# N1: 표 머리글 낱말들만으로 통째로 채워진 줄(칸이 한 줄로 뭉쳐 나온 경우)만
# 골라내기 위한 전체일치 패턴. 긴 낱말을 먼저 둬 "공종명칭"이 "공종명"에 먼저
# 먹히지 않게 한다.
_HEADER_ROW_RE = re.compile(
    r"^(?:공종코드|공종명칭|공종명|규격|단위|단가|노무비율|비고)+$"
)
DEFAULT6 = ["code", "name", "spec", "unit", "price", "labor"]
DEFAULT7 = DEFAULT6 + ["remark"]
FIELD_RE = re.compile(r"([가-힣]+(?:및[가-힣]+)*)분야자체표준시장단가")
# review_round3 wrong_notes(2024H2 p145, 2025H2 p95 "RH10* 접지공"): 분야 전환
# 표지 쪽("건축분야\n2025년 하반기 자체표준시장단가\n2025. 10")은 연도·반기 표시가
# "분야"와 "자체표준시장단가" 사이에 끼어들어(전체 쪽 글을 이어붙인 문자열에서
# "분야2025년하반기자체표준시장단가"), FIELD_RE(=page_field·records/pages.jsonl 값)
# 가 놓친다 — 그러면 표지 쪽 이후 다음 쪽(목차)까지 옛 그룹의 주석으로 잘못
# 이어붙었다(가로 2단 레이아웃은 우연히 다른 경로를 타 무사했다). page_field 값
# 자체는 건드리지 않고(불변 유지) "그룹 이어받기를 끊는다"는 판단에만 쓰는 별도
# 표지쪽 탐지(DIVISION_COVER_RE)를 둔다.
DIVISION_COVER_RE = re.compile(r"([가-힣]+(?:및[가-힣]+)*)분야(?:20\d{2}년[상하]반기)?자체표준시장단가")
# G02i2 F1: 원문 글자층에서 ①(U+2460) 대신 ⓛ(U+24DB, 동그라미 소문자 l) 로 인쇄된
# 항목이 있다(폰트 매핑 차이 — 화면엔 똑같이 "①"로 보인다). 글자는 원문대로 두되
# 항목 경계 판정에서는 ① 과 똑같이 다룬다.
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳ⓛ"
UNIT_NORM = {"주": "tree", "㎡": "m2", "㎥": "m3", "톤": "ton"}
BANNER_RE = re.compile(r"대분류\s*([A-Z])(?:\s*[,，]\s*([A-Z]))?\s*(.*)$")
BANNER_TRAIL_RE = re.compile(r"[·.…⋯]+.*$")
HALF_LABOR_RE = re.compile(r"^[‘'′`]?\d{2}[상하]")
_EV_ORDER = {"banner": 0, "group": 1, "table": 2, "notes": 3}
HALF_FMT_RE = re.compile(r"^20\d{2}H[12]$")

# N2: 그림·도식 속에 흩어진 낱말(화살표·거리표시·비교대괄호·품질등급만 나열)만 골라
# 잡는다. 정상 문장은 이 패턴에 걸리지 않도록 좁게 잡았다(문장부호·조사 없이
# 이 형태로만 이뤄진 한 줄일 때만).
DIAGRAM_ARROW_RE = re.compile(r"^[가-힣]{1,6}방향\s*→?(\s*\d+(?:\.\d+)?m)*$")
# review_round3 wrong_notes(2025H1 p106 JM잡철물): "<XG-21 규격>" 처럼 대괄호
# 안에 코드·숫자(영문·숫자·하이픈)가 섞인 줄은 예시도 비교 라벨("< 부적정 >"·
# "< 적정 >" 같은 순수 한글 평가어)이 아니라 바로 뒤에 오는 규격 목록의
# 표제이므로, 대괄호 안에 영문·숫자가 없을 때만 도식 라벨로 본다.
DIAGRAM_LABEL_RE = re.compile(r"^(?:\s*<[^<>0-9A-Za-z]{1,12}>\s*)+$")
# review_round3 score-dev-diff(2025H2 p89~90 "NA20* 수직구/토사굴착" ⑤):
# "< Ø10m 미만>"·"< Ø10m 이상>" 은 위 XG-21 예외와 반대로 "시공기준면"+깊이
# 눈금(5m/10m/15m)+코드로 이뤄진 진짜 예시도 두 벌을 구분하는 비교 라벨이다
# (Ø10m 미만/Ø10m 이상 두 단면을 나란히 보여주는 예시도의 캡션). "XG-21" 같은
# 제품 규격 코드(영문+하이픈+숫자)와 달리 치수(Ø·㎜·m)+비교어(이상/이하/미만/
# 초과)로만 이뤄진 대괄호는 여전히 도식 라벨로 본다.
# review_round4 score-dev-diff(2025H2 p89 재현): 이 쪽은 좌우 단이 아니라
# 한 폭 전체라 "< Ø10m 미만>"·"< Ø10m 이상>" 두 캡션이 같은 y(253.4)에 나란히
# 찍혀 한 줄로 뭉친다("< Ø10m 미만 > < Ø10m 이상 >") — 단일 대괄호만 허용하던
# "$" 앵커 탓에 이 뭉친 줄은 매치가 깨져 item⑤ 끝에 그대로 새어 붙었다.
# DIAGRAM_LABEL_RE(순한글) 처럼 "+"로 반복을 허용해 뭉친 줄도 잡는다.
DIAGRAM_DIM_LABEL_RE = re.compile(
    r"^(?:<\s*Ø?\s*\d+(?:\.\d+)?\s*(?:mm|㎜|cm|m)?\s*(?:이상|이하|미만|초과)\s*>\s*)+$"
)
# review_round1 wrong_notes(2025H2 p74·76): 예시도 범례가 세로로 한 칸씩
# 늘어서면(거리·등급이 한 줄에 하나씩) PyMuPDF 가 낱말마다 다른 y 로 뽑아
# _cluster_lines_y 가 한 줄에 하나씩만 담는다 — 예전엔 "2개 이상 반복"만 걸렀는데
# 그러면 이런 낱개 줄을 못 걸러 다른 문장에 뒤섞였다(1개도 인정).
DIAGRAM_DIST_ONLY_RE = re.compile(r"^(?:\d+(?:\.\d+)?m\s*)+$")
QUALITY_ONLY_RE = re.compile(r"^(?:양호|보통|불량)(?:\s+(?:양호|보통|불량))*$")
# N2: "< 부적정 >"·"< 적정 >" 처럼 대괄호가 옆 낱말과 떨어져 인식돼 낱말만
# 홀로 남는 경우(예시도의 사례 비교 라벨).
DIAGRAM_EVAL_ONLY_RE = re.compile(r"^(?:부적정|적\s*정)$")
# G02i2 F3: "부적정 (사유: 구간에 따라 암질별로 분류하여 구간별 적용)"처럼 평가어
# 뒤에 사유 괄호가 붙는 예시도 캡션(수량산출 예시도의 사례별 적정성 평가 줄).
DIAGRAM_EVAL_REASON_RE = re.compile(r"^(?:부적정|적정)\(사유[:：]")
# N2: 예시도 범례 안에서 낱개로 떨어진 공종코드(가격·설명 없이 코드만 한 줄).
DIAGRAM_CODE_ONLY_RE = re.compile(r"^[A-Z]{2}\d{3}\.\d{5}$")


class ValidationError(ValueError):
    """--half·--pages 같은 입력값이 잘못돼 추출을 시작할 수 없을 때(H3·H5)."""


def _validate_half(half: str, pdf_path: str | Path) -> list[str]:
    """--half 형식(^20\\d{2}H[12]$)을 검사하고, PDF 파일명에서 반기를 읽을 수 있으면 대조한다.

    형식이 틀리면 즉시 멈춘다(예외). 파일명과 다르면 막지 않고 경고만 돌려준다.
    """
    if not HALF_FMT_RE.match(half or ""):
        raise ValidationError(
            f"--half 형식이 올바르지 않습니다(예: 2025H2): {half!r}"
        )
    name = Path(pdf_path).name
    ym = re.search(r"(20\d{2})", name)
    half_word = "H1" if "상반기" in name else ("H2" if "하반기" in name else None)
    warnings: list[str] = []
    if ym and half_word:
        expected = f"{ym.group(1)}{half_word}"
        if expected != half:
            warnings.append(
                f"--half={half} 이(가) PDF 파일명에서 읽은 반기({expected})와 다릅니다: {name}"
            )
    return warnings


def _validate_pages(pages: tuple[int, int] | None, page_count: int) -> None:
    """--pages 의 역전(8-3)·0 이하·시작쪽이 PDF 쪽수를 넘는 경우를 조용히 넘어가지 않고 멈춘다."""
    if pages is None:
        return
    start, end = pages
    if start < 1 or end < 1:
        raise ValidationError(f"--pages 값은 1 이상이어야 합니다: {start}-{end}")
    if start > end:
        raise ValidationError(f"--pages 시작쪽이 끝쪽보다 큽니다(역전): {start}-{end}")
    if start > page_count:
        raise ValidationError(
            f"--pages 시작쪽 {start} 이(가) PDF 총 쪽수 {page_count}쪽을 넘습니다"
        )


@dataclass
class Span:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float = 0.0  # G02i2 F4: 글자 크기(폰트 pt) — PUA 윗첨자 판정에 쓴다.

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


# G02i2 F4: 4권의 "MA***** 타일공사" ③ 항목(2024H1 p103·2024H2 p206·2025H1
# p111·2025H2 p144)에 HyhwpEQ 폰트로 인쇄된 수식 글자가 PUA(U+E000~F8FF) 로
# 남는다. 화면(그림으로 직접 렌더해 확인, 코치 2026-09-17)은 "2.5×10⁴∼
# 1.0×10⁶Ω, 2.5×10⁴∼1.0×10⁶Ω" — 4권 모두 같은 문장·같은 코드값이다.
# 실물로 나타난 대응(직접 확인): U+E034→1 U+E035→2 U+E038→5 U+E03D→0
# U+E053→.(점) U+E037→4(윗첨자 자리) U+E039→6(윗첨자 자리). U+E036·U+E03A~E03C 는
# 이 4권에 실물로 나타나지 않아 확인하지 못했으므로 대응표에 넣지 않는다(코치
# 핫픽스 09-17) — 나타나면 그대로 남아 pua_chars 게이트가 잡는다.
_PUA_DIGIT_MAP = {
    "\uE034": "1", "\uE035": "2", "\uE037": "4", "\uE038": "5", "\uE039": "6", "\uE03D": "0",
}
_PUA_POINT_MAP = {"\uE053": "."}
_PUA_SUPER_MAP = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
}


def _fix_pua_spans(spans: list[Span]) -> None:
    """대응표에 있는 PUA 글자를 그 자리에서 원문 화면과 같은 숫자·점으로
    바꾼다. 같은 줄(y 6pt 이내)에서 다른 span 보다 작은(85% 미만) 크기로
    찍힌 숫자는 기준선이 올라간 윗첨자로 본다(원문 렌더 실측: 정상 8.52pt
    /윗첨자 5.76pt, 정상 12.0pt/윗첨자 8.16pt — 두 책 모두 비율 0.68). 대응표에
    없는 PUA 글자는 손대지 않는다(pua_chars 게이트가 남은 것을 잡는다)."""
    known = [s for s in spans if any(ch in _PUA_DIGIT_MAP or ch in _PUA_POINT_MAP for ch in s.text)]
    if not known:
        return
    known.sort(key=lambda s: s.yc)
    lines: list[list[Span]] = []
    for s in known:
        if lines and abs(s.yc - lines[-1][-1].yc) <= 6.0:
            lines[-1].append(s)
        else:
            lines.append([s])
    for line in lines:
        sizes = [s.size for s in line if s.size > 0]
        base = max(sizes) if sizes else 0.0
        for s in line:
            is_super = base > 0 and s.size > 0 and s.size < base * 0.85
            out_chars = []
            for ch in s.text:
                if ch in _PUA_DIGIT_MAP:
                    d = _PUA_DIGIT_MAP[ch]
                    out_chars.append(_PUA_SUPER_MAP[d] if is_super else d)
                elif ch in _PUA_POINT_MAP:
                    out_chars.append(_PUA_POINT_MAP[ch])
                else:
                    out_chars.append(ch)
            s.text = "".join(out_chars)


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
                out.append(Span(float(x0), float(y0), float(x1), float(y1), t, float(s.get("size") or 0.0)))
    _fix_pua_spans(out)
    return out


def _page_chars(page: fitz.Page) -> list[Span]:
    out: list[Span] = []
    rd = page.get_text("rawdict")
    for b in rd.get("blocks", []):
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                sz = float(s.get("size") or 0.0)
                for c in s.get("chars") or []:
                    ch = c.get("c") or ""
                    if ch == "":
                        continue
                    x0, y0, x1, y1 = c["bbox"]
                    out.append(Span(float(x0), float(y0), float(x1), float(y1), ch, sz))
    _fix_pua_spans(out)
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
                # 1행짜리 작은 표는 헤더/데이터 세로선이 ~19.8pt 로 끊긴다.
                if y1 - y0 > 15:
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


def _cluster_lines_y(spans: list[Span], gap: float = 4.0) -> list[tuple[float, str]]:
    """_cluster_lines() 와 같은 묶음 규칙이지만 (y중심, 이은 글) 을 함께 돌려준다.

    주석 안에서 작은 표(표 안 글을 y 순서로 끼워 넣기 위해) 위치를 알아야 할 때 쓴다.
    """
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
    return [(sum(x.yc for x in ln) / len(ln), _join_line(ln)) for ln in lines]


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


def _vline_crosses(wx0: float, wx1: float, real_vxs: list[float]) -> bool:
    """실제 세로선이 낱말 x 범위 안쪽을 가로지르는가. 추정 경계는 여기 넣지 않는다."""
    return any(wx0 + 0.35 < vx < wx1 - 0.35 for vx in real_vxs)


def _join_cell_text(word_spans: list[Span], char_spans: list[Span]) -> str:
    """같은 줄의 원문 낱말 사이에는 공백 한 칸. 잘린 낱말의 글자는 붙인다."""
    tagged: list[tuple[Span, str]] = [(s, "word") for s in word_spans] + [
        (s, "char") for s in char_spans
    ]
    if not tagged:
        return ""
    tagged.sort(key=lambda t: (t[0].y0, t[0].x0))
    lines: list[list[tuple[Span, str]]] = []
    for item in tagged:
        s = item[0]
        if not lines:
            lines.append([item])
            continue
        prev_yc = sum(x[0].yc for x in lines[-1]) / len(lines[-1])
        if abs(s.yc - prev_yc) <= max(4.0, (s.y1 - s.y0) * 0.45):
            lines[-1].append(item)
        else:
            lines.append([item])
    out: list[str] = []
    for ln in lines:
        ln.sort(key=lambda t: t[0].x0)
        tokens: list[str] = []
        buf: list[Span] = []

        def flush() -> None:
            if not buf:
                return
            t = _join_chars_line(buf)
            if t:
                tokens.append(t)
            buf.clear()

        for s, kind in ln:
            if kind == "word":
                flush()
                if s.text:
                    tokens.append(s.text)
            else:
                buf.append(s)
        flush()
        line = " ".join(t for t in tokens if t != "")
        if line.strip():
            out.append(line)
    return "\n".join(out)


def _cell_text(
    words: list[tuple[float, float, float, float, str]],
    chars: list[Span],
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    real_vxs: list[float],
) -> str:
    """칸 텍스트. 추정 경계로는 낱말을 자르지 않고 x 중심 칸에 통째로 넣는다.

    글자 단위 분할은 실제 세로선이 그 낱말을 가로지를 때만.
    잘린 낱말 bbox 가 옆 낱말과 겹치면, 안 잘린 낱말 쪽 글자는 버린다.
    같은 줄의 이웃 원문 낱말은 공백 한 칸으로 잇는다.
    """
    row: list[tuple[float, float, float, float, str]] = []
    for w in words:
        yc = (w[1] + w[3]) / 2.0
        if y0 < yc < y1:
            row.append(w)
    unsplit: list[tuple[float, float, float, float, str]] = []
    split: list[tuple[float, float, float, float, str]] = []
    for w in row:
        if _vline_crosses(w[0], w[2], real_vxs):
            split.append(w)
        else:
            unsplit.append(w)
    word_spans: list[Span] = []
    for wx0, wy0, wx1, wy1, t in unsplit:
        xc = (wx0 + wx1) / 2.0
        if x0 - 0.2 <= xc < x1:
            word_spans.append(Span(wx0, wy0, wx1, wy1, t))
    char_spans: list[Span] = []
    for wx0, wy0, wx1, wy1, t in split:
        if wx1 <= x0 or wx0 >= x1:
            continue
        for c in chars:
            if c.xc < wx0 - 0.4 or c.xc > wx1 + 0.4:
                continue
            if c.yc < wy0 - 0.4 or c.yc > wy1 + 0.4:
                continue
            if not (x0 - 0.2 <= c.xc < x1 and y0 < c.yc < y1):
                continue
            stolen = False
            for ux0, uy0, ux1, uy1, _ut in unsplit:
                if ux0 - 0.2 <= c.xc <= ux1 + 0.2 and uy0 - 0.2 <= c.yc <= uy1 + 0.2:
                    stolen = True
                    break
            if stolen:
                continue
            char_spans.append(c)
    if not word_spans and not char_spans:
        return ""
    return _join_cell_text(word_spans, char_spans)


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


def _is_half_labor(t: str) -> bool:
    return bool(HALF_LABOR_RE.match((t or "").strip().replace(" ", "")))


def _major_name_from(raw: str) -> str:
    """배너 이름: 글자 사이 공백 제거, 괄호 유지. 목차 점선·쪽번호 제거."""
    t = BANNER_TRAIL_RE.sub("", raw or "")
    t = re.sub(r"\d+\s*$", "", t)
    t = t.replace("■", "")
    t = re.sub(r"\s+", "", t)
    if CODE_FIND.search(t) or "공종코드" in t or "공종명" in t:
        return ""
    if len(t) > 24:
        return ""
    if not re.search(r"[가-힣]{2,}", t):
        return ""
    return t


def _parse_banner_line(line: str) -> tuple[str, str] | None:
    """합쳐진 배너는 major=첫 글자, 이름은 나머지. 대분류Q, R 기타공사 → (Q, 기타공사)."""
    if "대분류" not in (line or "").replace(" ", ""):
        return None
    for src in (line, re.sub(r"\s+", "", line)):
        m = BANNER_RE.search(src)
        if m:
            return m.group(1), _major_name_from(m.group(3) or "")
    return None


def _banner(spans: list[Span], x0: float, x1: float) -> list[tuple[float, str, str]]:
    """(yc, letter, name) 대분류 배너. 같은 줄(대분류 D 토공사) 우선, 없으면 아래 줄."""
    seeds = [s for s in spans if x0 - 1 <= s.xc < x1 + 1 and "대분류" in s.text]
    found: list[tuple[float, str, str]] = []
    for s in seeds:
        line = _line_text_at(spans, s.yc, x0, x1, tol=12)
        parsed = _parse_banner_line(line) or _parse_banner_line(s.text)
        if parsed is None:
            continue
        letter, name = parsed
        if not name:
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
            name = _major_name_from(below_txt)
        found.append((s.yc, letter, name))
    found.sort(key=lambda it: it[0])
    uniq: list[tuple[float, str, str]] = []
    for item in found:
        if not uniq or abs(item[0] - uniq[-1][0]) > 20:
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
    if s == "폐기":
        return True
    if LABOR_RE.match(s):
        return True
    if HALF_LABOR_RE.match(s):
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


# 코치 핫픽스 09-17: 「[표준도] 우수맨홀(900×900) …」 줄도 그림 제목이다(표준도 그림
# 바로 위 제목 줄). [그림 과 같게 figures 로 보낸다 — 주석 ③ 끝에 붙던 결함.
FIGURE_CAPTION_PREFIXES = ("[그림", "[표준도]")


def _is_figure_caption_line(text: str) -> bool:
    return text.lstrip().startswith(FIGURE_CAPTION_PREFIXES)


def _figure_captions_from_words(
    words: list[tuple[float, float, float, float, str]],
    x0: float,
    x1: float,
    y0: float,
    y1: float,
) -> list[str]:
    """줄 첫 낱말이 [그림 으로 시작하는 줄을 공백으로 이어 제목으로 만든다."""
    picked = [
        w
        for w in words
        if x0 - 1 <= (w[0] + w[2]) / 2 < x1 + 1 and y0 < (w[1] + w[3]) / 2 < y1
    ]
    if not picked:
        return []
    picked = sorted(picked, key=lambda w: ((w[1] + w[3]) / 2.0, w[0]))
    lines: list[list[tuple[float, float, float, float, str]]] = []
    for w in picked:
        yc = (w[1] + w[3]) / 2.0
        if not lines:
            lines.append([w])
            continue
        prev_yc = sum((x[1] + x[3]) / 2.0 for x in lines[-1]) / len(lines[-1])
        if abs(yc - prev_yc) <= max(4.0, (w[3] - w[1]) * 0.45):
            lines[-1].append(w)
        else:
            lines.append([w])
    caps: list[str] = []
    for ln in lines:
        ln = sorted(ln, key=lambda w: w[0])
        if ln and ln[0][4].startswith(FIGURE_CAPTION_PREFIXES):
            cap = " ".join(w[4] for w in ln if w[4]).strip()
            if cap:
                caps.append(cap)
    return caps


def _header_row_trim(
    lo: float, hi: float, headers: list[tuple[float, dict, float, float]]
) -> float:
    """주석이 새 ■ 없이 바로 다음 표로 이어지는 자리에서, 그 표의 칸 이름 줄
    (공종코드·공종명칭…) 위쪽에서 끊는다.

    다음 표의 첫 marker(코드·소제목) yc 를 그대로 경계로 쓰면, 이름 칸이 여러
    줄로 접혀(예: "트렌치커버/\\n아연도그레이팅(무소음)") 행 안에서 코드보다 더
    위쪽에 인쇄된 이름 칸 첫 줄까지 주석 band 안에 들어와, 앞 항목 끝에 다음
    레코드의 명칭 첫 줄이 그대로 새어 붙는다(review_round3 wrong_notes: 2025H1
    p103 "JG****** 금속덮개", 2025H2 p83 "ND10* 본선 시설공" — 두 반기·세 곳
    이상에서 재현된 시스템적 결함). 이 구간(lo~hi) 안에 표 칸 이름 줄이 있으면
    그 줄의 위쪽 끝(hy0)에서 끊는다.
    """
    best = hi
    for hyc, _labels, _hy1, hy0 in headers:
        if lo < hyc < hi + 40:
            best = min(best, hy0 - 1.0)
    return best


def _note_items(lines: list[str], pdf_page: int) -> list[dict]:
    """주석 줄 목록 -> 항목 목록.

    N1: 줄바꿈으로 갈린 항목의 마지막 조각(예 "…포함" 뒤 "한다.")이 짧다는
    이유만으로 버려지지 않도록, "너무 짧고 한글 3연속 없음" 같은 약한 잡음
    판정(_is_note_junk)은 buf 가 비어 새 항목을 시작하려 할 때만 적용한다.
    이미 항목이 진행 중인 이어지는 줄에는 항상 안전한 구조적 잡음 판정
    (_is_structural_note_junk: 쪽번호·대분류 배너·낱개 코드·그림 속 낱말)만 적용한다.
    item 은 원문 줄바꿈 자리를 공백 하나로 이은 한 줄, item_raw 는 같은 자리에
    \\n 을 둔 원문 층이다.
    """
    items: list[dict] = []
    parts: list[str] = []

    def flush() -> None:
        if not parts:
            return
        item = _trim_note(" ".join(parts))
        item_raw = _trim_note("\n".join(parts))
        if item and not _is_note_junk(item):
            items.append({"item": item, "item_raw": item_raw, "pdf_page": pdf_page})
        parts.clear()

    for line in lines:
        s = line.strip()
        if not s or s.startswith("【단가정의】") or s.startswith("단가정의"):
            continue
        if _is_figure_caption_line(s):
            continue
        if s.startswith("(표)"):
            flush()
            items.append({"item": s, "item_raw": s, "pdf_page": pdf_page, "subtable": True})
            continue
        is_new_item = bool(s) and (s[0] in CIRCLED or s.startswith("※"))
        if is_new_item:
            flush()
            rest = s[1:].lstrip()
            parts.append(f"{s[0]} {rest}" if rest else s[0])
            continue
        if parts:
            # 이미 항목이 진행 중: 확실한 구조적 잡음만 걸러내고, 그 외 짧은
            # 줄(문장 끝 조각)은 그대로 이어붙인다(N1).
            if _is_structural_note_junk(s):
                continue
            parts.append(s)
        else:
            if _is_note_junk(s):
                continue
            parts.append(s)
    flush()
    return items


def _trim_note(s: str) -> str:
    t = re.sub(r"\s*-\s*\d+\s*-\s*$", "", s.strip())
    return t.strip()


def _merge_note_continuations(notes: list[dict]) -> list[dict]:
    """G02i2 F1: 항목 기호 없이 시작하는 항목은 앞 항목의 이어짐이다.

    attach_notes_from() 는 쪽·단·반쪽 경계마다 따로 _note_items() 를 부른다
    (그때마다 buf 상태가 새로 시작한다). 경계에서 잘린 항목의 뒷부분(예: p17
    "…되는" 뒤 p18 "모든 비용을 포함한다.")은 항목 기호 없는 독립 항목으로
    그룹 notes 에 그대로 들어간다 — 그룹 자체는 last_group 이 쪽을 넘어
    이어받으므로(1568줄) 같은 그룹 notes 리스트 안에 있다. 문서 전체를 다
    읽은 뒤(여러 쪽에 걸친 그룹도 notes 가 다 모인 뒤) 한 번에, 기호 없이
    시작하는 항목을 바로 앞의 "글 항목"에 문장으로 잇는다.

    (표) 부표 항목은 그 자체가 하나의 항목 경계이지만(kepco_notes_items.py
    BULLET_RE 도 "(표)"를 허용), 부표 뒤에 이어지는 기호 없는 글은 부표가
    아니라 부표 앞의 글 항목이 계속되는 것이다(부표는 그 항목 문장 속에 얹힌
    서식일 뿐 새 항목이 아니다) — 그래서 "마지막 글 항목" 포인터는 부표를
    지나쳐도 갱신하지 않는다.
    """
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
                # 그룹의 맨 첫 항목이 기호 없이 시작 — 이어붙일 앞 항목이
                # 없는 참 구조적 이상(고아). 그대로 두어 scan 도구가 잡게 한다.
                last_text_idx = len(merged) - 1
            continue
        target = merged[last_text_idx]
        target["item"] = _trim_note(f"{target['item']} {item}")
        target["item_raw"] = f"{target.get('item_raw', target['item'])}\n{n.get('item_raw', item)}"
    return merged


def _is_structural_note_junk(s: str) -> bool:
    """항상(이어지는 줄이라도) 걸러야 하는 확실한 잡음.

    쪽번호·대분류 배너 잔여·낱개 코드 파편·표 잔여 "/"·그림·도식 속에 흩어진
    화살표/거리표시/비교대괄호/품질등급-only 줄(N2)이 대상이다. 길이만으로
    판단하는 약한 규칙은 여기 없다 — 그건 항목을 새로 시작할 때만(_is_note_junk).
    """
    t = s.strip()
    if not t:
        return True
    if t[0] in CIRCLED or t.startswith("(표)"):
        return False
    compact = t.replace(" ", "")
    if re.fullmatch(r"-\s*\d+\s*-", t) or re.fullmatch(r"-\d+-", compact):
        return True
    if t.startswith("대분류") or compact.startswith("대분류"):
        return True
    if FIELD_RE.search(compact) or "자체표준시장단가" in compact:
        # 분야 전환 표지("건축·기계설비분야 2023년 하반기 자체표준시장단가 2024. 2")
        # 는 그 분야 전환이 그룹의 마지막 표와 같은 쪽(가로판형 좌우 2단)에서
        # 일어나면(new_group 의 last_group 리셋이 다음 쪽부터만 걸리므로) 직전
        # 그룹의 notes 에 붙을 수 있다 — 목차·표지 문구이므로 어느 그룹의 주석에도
        # 넣지 않는다. "건축․기계설비분야"처럼 가운뎃점이 특수 문자(U+2024 등)라
        # FIELD_RE 가 못 잡는 줄도 있어 "자체표준시장단가" 부분일치를 함께 본다.
        return True
    if re.fullmatch(r"[가-힣·ㆍ‧･․]{2,12}분야", compact):
        return True
    if re.fullmatch(r"\d{4}\.\s*\d{1,2}", t):
        # 분야 표지의 발행연월 줄("2024. 2")
        return True
    if re.match(r"^[A-Z]{2}\d+\*", compact) and len(compact) < 28:
        return True
    if STAR_FIND.fullmatch(compact) or re.fullmatch(r"[A-Z]{2}\d+\*+", compact):
        return True
    if t.endswith("/") and len(t) < 40:
        return True
    if re.fullmatch(r"<사례\d+>", compact):
        # G02i2 F3: "<사례1>"·"<사례2>" 는 수량산출 예시도(굴진방향 도식) 자체의
        # 사례 번호 라벨이다(원문에서도 그 도식 바로 위에 독립된 한 줄로 찍힌다).
        # 코치가 원문·정답지를 다시 대조해(09-17) "…(Ø2,000㎜ 적용 예)" 로 항목이
        # 끝나고 그 뒤엔 항목이 없어야 한다고 정정했다 — 그림 라벨로 뺀다.
        return True
    if DIAGRAM_EVAL_REASON_RE.match(compact):
        # G02i2 F3: "부적정 (사유: …)"·"적  정 (사유: …)" 처럼 사유가 붙은 평가
        # 줄도 예시도 캡션이다(DIAGRAM_EVAL_ONLY_RE 는 사유 없는 낱말만 잡는다).
        return True
    if (
        DIAGRAM_ARROW_RE.match(t)
        or DIAGRAM_LABEL_RE.match(t)
        or DIAGRAM_DIM_LABEL_RE.match(t.replace(" ", ""))
        or DIAGRAM_DIST_ONLY_RE.match(compact)
        or QUALITY_ONLY_RE.match(t)
        or DIAGRAM_EVAL_ONLY_RE.match(t)
        or DIAGRAM_CODE_ONLY_RE.match(compact)
    ):
        return True
    # N2: 예시도 안 코드+거리구간+품질등급 라벨 낱말(예: "0-150m이내, 양호",
    # 순서가 뒤섞인 "이내150-370m,보통")은 find_tables() 표로도 안 잡히고 위
    # 개별 패턴에도(범위+등급이 한 조각에 같이 있어) 안 걸린다. 짧고(≤20자)
    # 거리구간과 품질등급이 함께 있을 때만 그림 라벨로 본다(긴 서술문은 그대로 둔다).
    if len(compact) <= 20 and re.search(r"(양호|보통|불량)", compact) and re.search(
        r"\d+(?:\.\d+)?\s*[~∼\-]\s*\d+(?:\.\d+)?m", compact
    ):
        return True
    # 원문에서 새 항목으로 시작하는 줄머리 기호(대괄호 표준도 라벨 등)만 있고
    # 그 안에 실제 문장이 없는 줄
    if re.fullmatch(r"\[[^\[\]]{1,20}\]", t):
        return True
    # 코드 접두 파편(예: "ED**"·"LC***"·"ED*** -" — 숫자 없이 별표만 붙은
    # 대분류 코드 조각. 표 잔여 하이픈이 뒤에 붙는 경우도 있다)
    if re.fullmatch(r"[A-Z]{2}\*+-*", compact):
        return True
    if t.startswith("- ") and re.search(r"(총연장|초과|이하)", t) and "단가" not in t:
        return True
    # G02i2 2차 T09(코치 독립 시험, 2025H1 p97~121 검정 채점 실패): "- 초기굴진"·
    # "- 본굴진"·"- 도달굴진" 처럼 총연장/초과/이하 낱말이 없는 표 소구간 표제도
    # 있다(2025H1 p122 "ND10* 콘크리트관 추진" 그룹, 레코드 표 안 소제목이지
    # 【단가정의】 시작 전이라 표 몸통에 못 잡히면 다음 쪽 진짜 항목 앞에
    # 가짜 항목으로 샜다). conservation.py 의 같은 이름 게이트(_note_line_is_
    # structural)에는 이미 있던 정확 일치 규칙을 여기(추출 코드)에도 그대로
    # 옮긴다 — 두 파일이 서로 다른 기준으로 갈라져 있었다.
    if re.fullmatch(r"-\s*(초기굴진|본굴진|도달굴진)", t):
        return True
    return False


def _is_note_junk(s: str) -> bool:
    """새 항목을 시작하거나 독립된 한 줄로 볼 때 적용하는 잡음 판정.

    이어지는 줄에는 이 중 "너무 짧고 한글 3연속 없음" 규칙을 적용하지
    않는다(_is_structural_note_junk 가 그 대신 쓰인다) — N1 참고.
    """
    t = s.strip()
    if _is_structural_note_junk(t):
        return True
    if t[0] in CIRCLED or t.startswith("(표)") or t.startswith("<"):
        return False
    compact = t.replace(" ", "")
    compact = re.sub(r"-\d+-", "", compact)
    compact = compact.replace("()", "").strip("-").strip()
    if not compact:
        return True
    if len(compact) < 24 and not re.search(r"[가-힣]{3,}", t):
        return True
    return False


def _bbox_overlap_ratio(inner: tuple, outer: tuple) -> float:
    """inner 사각형이 outer 와 겹치는 넓이 비율(inner 기준)."""
    ax0, ay0, ax1, ay1 = inner
    bx0, by0, bx1, by1 = outer
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    area = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
    return inter / area


def _is_diagram_table(
    tb: tuple,
    full_txt: str,
    row_count: int,
    image_boxes: list[tuple],
) -> bool:
    """N2: find_tables() 가 표로 잘못 잡은 그림·도식 상자인가.

    (a) 래스터 이미지와 겹치는 상자(예: 시공기준면 예시도의 적정 쪽), (b) 비교
    대괄호 "<"·">" 를 담고 있는 상자(예: "< 부적정 >"·"< 적정 >"), (c) 1행짜리
    표에 단가 없는 코드와 거리표시(2m·4m·6m 등)만 있는 범례(예: 시공기준면
    예시도의 부적정 쪽, 이미지가 없다) 를 그림으로 본다.
    """
    for ib in image_boxes:
        if _bbox_overlap_ratio(tb, ib) > 0.2 or _bbox_overlap_ratio(ib, tb) > 0.2:
            return True
    if "<" in full_txt and ">" in full_txt:
        return True
    if row_count <= 1 and CODE_FIND.search(full_txt) and re.search(r"\d+(?:\.\d+)?m(?!\S)", full_txt):
        return True
    # (d) 수량산출 예시도(사례2 등): 코드 + 거리구간(50m~100m, L=70m～100m이하 등)
    # 표시로 구성되고 단가(콤마 붙은 금액)가 전혀 없는 표는 실제 가격표가 아니라
    # 예시도가 find_tables() 에 표로 잘못 잡힌 것이다(2025H2 p74·76 "사례2",
    # p85·86 "부적정/적정" 예시 — review_round1 wrong_notes 반증).
    has_price_comma = bool(re.search(r"\d{1,3}(?:,\d{3})+", full_txt))
    has_dist_range = bool(
        re.search(r"\d+(?:\.\d+)?m\s*[~∼\-]\s*\d+(?:\.\d+)?m", full_txt)
        or re.search(r"L\s*=\s*\d+(?:\.\d+)?m", full_txt)
        # "550-750m"·"150-370m" 처럼 뒤쪽 숫자에만 m 이 붙는 구간 표기(사례2 예시도)
        or re.search(r"\d+(?:\.\d+)?\s*[~∼\-]\s*\d+(?:\.\d+)?m(?!\S)", full_txt)
    )
    if CODE_FIND.search(full_txt) and has_dist_range and not has_price_comma:
        return True
    return False


def _cell_text_from_words(
    words: list[tuple[float, float, float, float, str]], bbox: tuple[float, float, float, float]
) -> str:
    """칸 bbox 안 낱말을 읽기순서(y줄→x)로 이어 붙인다. 원문 낱말에 붙은
    글자(쉼표 등)를 그대로 보존한다 — table.extract() 의 재구성 텍스트를
    쓰지 않고 이 파일 자체의 words 로 다시 읽는다."""
    x0, y0, x1, y1 = bbox
    cell_spans = [
        Span(wx0, wy0, wx1, wy1, t)
        for wx0, wy0, wx1, wy1, t in words
        if x0 - 1.0 <= (wx0 + wx1) / 2.0 <= x1 + 1.0 and y0 - 1.0 <= (wy0 + wy1) / 2.0 <= y1 + 1.0
    ]
    if not cell_spans:
        return ""
    lines = _cluster_lines_y(cell_spans, gap=4.0)
    lines.sort(key=lambda t: t[0])
    return " ".join(txt for _, txt in lines if txt)


def _geom_note_subtables(
    hx0: float,
    hx1: float,
    y0: float,
    y1: float,
    hlines: list[LineSeg],
    vlines: list[LineSeg],
    words_all: list[tuple[float, float, float, float, str]],
    claimed_boxes: list[tuple[float, float, float, float]],
) -> list[tuple[float, str, tuple[float, float, float, float]]]:
    """F2(wrong_notes 2024H2#p177#233): find_tables() 가 작은(2행2열 등) 소표를
    페이지에 따라 통째로 놓칠 때(같은 문구·같은 표 크기의 다른 반기 페이지는
    잡는데, 이 페이지의 조판만 놓친다 — PyMuPDF 표 인식기의 레이아웃 의존
    회귀), 그 표의 격자선(page.get_drawings() 로 이미 뽑아 둔 hlines·vlines)을
    직접 읽어 같은 "(표) 칸|칸 / 칸|칸" 형식을 만든다. find_tables() 유무와
    무관하게 항상 같은 규칙으로 돈다 — 특정 쪽·그룹을 이름으로 골라내지 않는다.

    격자 판정: 가로선을 (좌단 x, 우단 x) 가 서로 2.5pt 이내로 같은 것끼리
    묶는다(표의 위·중간·경계선들은 폭이 같다). 그 묶음의 서로 다른 y 가 2개
    이상(행 경계 ≥2, 즉 데이터 행 ≥1)이고, 그 폭·y범위 안에 세로선이 1개
    이상 지나가면(칸 ≥2) 표 후보로 본다. 이미 find_tables() 나 그림으로 처리된
    상자와 크게 겹치면 건너뛴다(중복 검출 방지). 칸 낱말이 하나도 없는(빈
    장식 사각형) 후보는 버린다.
    """
    cand = [
        h
        for h in hlines
        if y0 - 2 <= h.c <= y1 + 2 and h.a >= hx0 - 3 and h.b <= hx1 + 3 and 15 <= (h.b - h.a) <= (hx1 - hx0) * 0.85
    ]
    groups: list[tuple[float, float, list[float]]] = []  # (a, b, [ys])
    for h in cand:
        for i, (ga, gb, ys) in enumerate(groups):
            if abs(ga - h.a) <= 2.5 and abs(gb - h.b) <= 2.5:
                ys.append(h.c)
                break
        else:
            groups.append((h.a, h.b, [h.c]))
    out: list[tuple[float, str, tuple[float, float, float, float]]] = []
    for ga, gb, ys in groups:
        row_ys = _cluster_xs(ys, tol=1.5)
        if len(row_ys) < 2:
            continue
        row_ys.sort()
        box = (ga, row_ys[0], gb, row_ys[-1])
        if any(_bbox_overlap_ratio(box, cb) > 0.3 for cb in claimed_boxes):
            continue
        vxs = _vxs_near(vlines, row_ys[0], row_ys[-1], ga, gb)
        col_xs = [ga] + vxs + [gb]
        if len(col_xs) < 3 or len(col_xs) > 6:
            continue
        rows_cells: list[list[str]] = []
        for ri in range(len(row_ys) - 1):
            cells = []
            for ci in range(len(col_xs) - 1):
                cb = (col_xs[ci], row_ys[ri], col_xs[ci + 1], row_ys[ri + 1])
                cells.append(_cell_text_from_words(words_all, cb))
            rows_cells.append(cells)
        if not any(c for row in rows_cells for c in row):
            continue
        blob = " ".join(c for row in rows_cells for c in row)
        if "대분류" in blob.replace(" ", "") or "공종코드" in blob:
            continue
        parts = [" | ".join(c for c in row if c) for row in rows_cells if any(row)]
        body = " / ".join(p for p in parts if p)
        if not body:
            continue
        out.append((row_ys[0], "(표) " + body, box))
    return out


def _flatten_subtable(
    table,
    words_all: list[tuple[float, float, float, float, str]] | None = None,
) -> str:
    """주석 안 작은 표: "(표) " + 행은 " / ", 칸은 " | ".

    N2: 표 안에 그림 범례 행(예: "굴진방향→ 50m 100m")이 섞여 있으면 그 행만
    빼고 나머지 진짜 표 내용(예: "보통토사 | 고사점토 / ND109… ")은 그대로 둔다
    — 표 전체를 지우지 않는다(review 09-17: 사례별 예시 표는 정답지가 (표)로 둔다).

    G02i2 2차(코치 재검, 2024H1 p45#107 "ED*** 거푸집" ②): find_tables() 의
    table.extract() 는 칸 안에서 줄이 바뀌는 자리("옹벽,"→"파라펫트,")의 낱말
    끝 문장부호를 이따금 통째로 빠뜨린다(PyMuPDF 자체의 재구성 문제, 이 표
    바깥 낱말 원자료 words 에는 "옹벽," 그대로 있음). 줄바꿈이 있던(즉 "\\n"
    이 있던) 칸만, 그 칸의 실제 bbox(t.rows[i].cells[j])로 이 파일의 words
    를 다시 읽어 원문 문장부호를 되살린다. words_all 이 없거나(호출부에서
    안 넘기면) 칸 bbox 를 못 구하면 기존 방식(빈칸으로 이어붙이기)을 그대로
    쓴다 — 표 안 다른 모든 칸(줄바꿈 없는 대다수)의 동작은 바뀌지 않는다.
    """
    try:
        rows = table.extract()
    except Exception:
        return ""
    cell_bboxes: list[list[tuple[float, float, float, float]]] | None = None
    if words_all is not None:
        try:
            cell_bboxes = [list(r.cells) for r in table.rows]
        except Exception:
            cell_bboxes = None
    parts = []
    for ri, row in enumerate(rows):
        cells = []
        for ci, c in enumerate(row):
            raw = c or ""
            has_bbox = cell_bboxes is not None and ri < len(cell_bboxes) and ci < len(cell_bboxes[ri])
            bx = cell_bboxes[ri][ci] if has_bbox else None
            if "\n" in raw and bx is not None:
                rebuilt = _cell_text_from_words(words_all, bx)
                if rebuilt:
                    raw = rebuilt
            elif raw.strip() and not re.search(r"[0-9A-Za-z가-힣]", raw) and bx is not None:
                # G02i2 2차(코치 재검, 2024H1 p45#107 "비고" 칸): table.extract()
                # 가 실제 원문 낱말이 하나도 없는데도 순수 문장부호(예: 쉼표
                # 하나)를 칸 값으로 내놓는 경우가 있다(옆 칸 낱말 끝 글자가
                # 칸 경계를 살짝 넘어가는 PyMuPDF 자체 재구성 artifact로
                # 추정 — 이 파일의 words 에는 그 위치에 어떤 글자도 없다).
                # 글자·숫자·한글이 전혀 없는 순수 문장부호 칸만, 그 bbox 안에
                # 실제 낱말이 하나도 없으면 빈칸으로 되돌린다(정말 문장부호
                # 하나가 칸 값인 경우는 words 에도 그 글자가 있어 안 바뀐다).
                if not _cell_text_from_words(words_all, bx):
                    raw = ""
            cells.append(re.sub(r"\s+", " ", raw.replace("\n", " ")).strip())
        if not any(cells):
            continue  # 완전히 빈 행(그림 상자의 여백 행)
        if any(DIAGRAM_ARROW_RE.match(c) for c in cells if c):
            continue  # 그림 범례 화살표 행("굴진방향→ 50m 100m" 등)
        parts.append(" | ".join(cells))
    body = " / ".join(p for p in parts if p.strip("| "))
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


def _scan_division_covers(doc: fitz.Document) -> set[int]:
    """분야 전환 표지 쪽 번호 집합(review_round3 wrong_notes: 2024H2 p145,
    2025H2 p95 "RH10* 접지공"). field_starts(=page_field, records/pages.jsonl 에
    그대로 쓰이는 값)와는 별도로만 쓴다 — 이 집합은 오직 "이 쪽에서 그룹
    이어받기를 끊는다"는 판단에만 쓰고, page_field 값 자체는 절대 바꾸지
    않는다(그래야 records.jsonl·pages.jsonl 바이트 동일 불변이 유지된다)."""
    covers: set[int] = set()
    for i in range(doc.page_count):
        compact = doc[i].get_text("text").replace(" ", "").replace("\n", "")
        if DIVISION_COVER_RE.search(compact):
            covers.add(i + 1)
    return covers


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
    doc = fitz.open(pdf_path)
    try:
        half_warnings = _validate_half(half, pdf_path)
        _validate_pages(pages, doc.page_count)
    except Exception:
        doc.close()
        raise
    sha = _sha256(pdf_path)
    field_starts = _scan_fields(doc)
    division_covers = _scan_division_covers(doc)
    start, end = (1, doc.page_count) if pages is None else pages
    start = max(1, start)
    end = min(doc.page_count, end)

    records: list[dict] = []
    groups: list[dict] = []
    subheaders: list[dict] = []
    pages_out: list[dict] = []
    warnings: list[str] = list(half_warnings)

    last_group: dict | None = None
    last_colmap: dict[str, tuple[float, float]] | None = None
    last_major = ""
    last_major_name = ""
    group_seq = 0
    # H1: 표가 쪽·단을 넘어 이어질 때도 직전 소제목(＊)·직전 명칭을 이어받는다.
    # last_group·last_colmap 과 같은 원리로, 새 ■ 그룹이 시작될 때만 끊는다(new_group 참고).
    last_sub: tuple[str, str] | None = None
    last_prev_name = ""

    # --pages 중간부터여도 앞쪽 배너를 읽어 대분류를 이어받는다
    if start > 1:
        for pno in range(1, start):
            page = doc[pno - 1]
            _layout, halves_pre = _halves(page)
            spans_pre = _page_spans(page)
            for _ph, hx0, hx1 in halves_pre:
                spans = [s for s in spans_pre if _in_half(s, hx0, hx1)]
                for _yc, letter, name in _banner(spans, hx0, hx1):
                    last_major = letter
                    last_major_name = name

    for pno in range(start, end + 1):
        page = doc[pno - 1]
        layout, halves = _halves(page)
        page_field = _field_at(field_starts, pno)
        # 분야 전환(토목 -> 건축·기계설비 등) 감지: 직전 그룹이 이전 분야 것이면
        # 표지·목차·표준시장단가 목록(색인) 같은 무관한 내용이 그 그룹의 notes 에
        # 통째로 이어붙는 것을 막는다(N1~N3 과 무관한 별도 결함, review_round1
        # code_issues severity=high). 새 분야의 첫 ■ 그룹이 나오기 전까지는
        # 그 사이 글줄을 어느 그룹에도 붙이지 않는다.
        if last_group is not None and last_group.get("field") not in (None, page_field):
            last_group = None
        # review_round3: page_field(=FIELD_RE) 판정이 한 쪽 늦게 걸리는 분야 전환
        # 표지 쪽(위 DIVISION_COVER_RE 주석 참고)에서도 그룹 이어받기를 끊는다.
        # page_field 값 자체는 그대로 두어(records.jsonl·pages.jsonl 불변 유지)
        # 오직 이 판단에만 쓴다.
        if pno in division_covers:
            last_group = None
        spans_all = _page_spans(page)
        chars_all = _page_chars(page)
        words_all = [
            (float(w[0]), float(w[1]), float(w[2]), float(w[3]), w[4])
            for w in page.get_text("words")
            if w[4]
        ]
        hlines, vlines = _page_lines(page)
        try:
            image_boxes = [tuple(im["bbox"]) for im in page.get_image_info()]
        except Exception:
            image_boxes = []
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

            # y-ordered events: 배너 + group headers + clusters + 단가정의 라벨
            dangas = [s for s in spans if _is_danga_label(s.text)]
            events: list[tuple[float, str, object]] = []
            for yc, letter, name in banners:
                events.append((yc, "banner", (letter, name)))
            for g in gheads:
                events.append((g.yc, "group", g))
            for cl in clusters:
                events.append((cl[0][1].yc, "table", cl))
            for d in dangas:
                events.append((d.yc, "notes", d))
            events.sort(key=lambda e: (e[0], _EV_ORDER.get(e[1], 9)))

            current_group = None  # assigned after first ■ on this half
            half_started = False

            def new_group(header_span: Span) -> dict:
                nonlocal group_seq, last_group, last_major, last_sub, last_prev_name
                group_seq += 1
                # 새 ■ 그룹은 새 주제이므로 직전 그룹의 소제목·명칭 이어받기를 끊는다.
                last_sub = None
                last_prev_name = ""
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
                    "figures": [],
                    "record_count": 0,
                }
                groups.append(g)
                last_group = g
                return g

            def attach_notes_from(y0: float, y1: float, grp: dict | None) -> None:
                if grp is None:
                    return
                # lines between y0 and y1, excluding table codes already handled
                table_entries: list[tuple[float, str]] = []
                figure_boxes: list[tuple[float, float, float, float]] = []
                # detect subtables via find_tables (band 을 자르기 전에 먼저 훑어
                # 그림 상자를 찾아둔다 — 그래야 그 상자 안 글자를 raw_lines 에서 뺄 수 있다)
                for t in tables:
                    tb = t.bbox
                    # review_round3 wrong_notes(2025H1 p76/78 NA10* 잔토처리):
                    # tables 는 쪽 전체(양쪽 단)에서 찾은 결과라, 이 단(hx0~hx1)의
                    # x 범위를 먼저 걸러두지 않으면 반대쪽 단의 진짜 가격표(이미
                    # records.jsonl 에 정상 포함된 표)까지 y 만 맞으면 이 단의 주석
                    # 후보로 잡혀 통째로 "(표)" 로 중복 게재된다 — ncodes 판정은
                    # 이 단의 rec_codes 만 보므로 반대쪽 단 표에는 항상 0으로 나와
                    # 제외되지 않았다. 겹치는 x 범위가 없으면(다른 단의 표) 먼저 건너뛴다.
                    if tb[2] <= hx0 + 1 or tb[0] >= hx1 - 1:
                        continue
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
                        # N3: 대분류 배너 상자는 몇 글자든(한 글자·"Q, R" 두 글자 모두)
                        # 주석 표에 넣지 않는다.
                        figure_boxes.append(tb)
                        continue
                    ncodes = 0
                    for s in rec_codes:
                        if tb[0] - 5 <= s.xc <= tb[2] + 5 and tb[1] <= s.yc <= tb[3]:
                            ncodes += 1
                    if ncodes >= 1 and t.col_count >= 5:
                        figure_boxes.append(tb)  # 실제 표(다음 쪽 이어짐 등)의 중복 캡처 방지
                        continue
                    # 배너처럼 열이 지나치게 많은 표는 제외
                    if t.col_count >= 12:
                        figure_boxes.append(tb)
                        continue
                    # 품질등급 헤더 표(양호/보통/불량 조합)는 두 갈래다.
                    # (a) 사례1처럼 "보통"·"불량"이 모두 있는 온전한 범례는 실제
                    # 예시 표(코드+거리구간+등급 조합)이지 그림이 아니다 — 09-17
                    # 재검한 정답지(gold_dev)가 이 형태를 (표)로 그대로 둔다.
                    # (b) 사례2처럼 "양호"만 반복돼 보통·불량이 아예 없는 퇴화한
                    # 헤더는 find_tables() 가 예시도를 잘못 표로 잡은 것이다
                    # (review_round1 wrong_notes 2025H2 p74·76: 내용이 없거나
                    # "품셈으로 산출"만 있는 가짜 서브테이블).
                    row0 = [re.sub(r"\s+", "", str(c or "")) for c in ext0]
                    labels = {"양호", "보통", "불량"}
                    is_quality_header = (
                        t.col_count >= 4
                        and bool(row0)
                        and all(x in labels or x == "" for x in row0)
                        and any(x in labels for x in row0)
                    )
                    # G02i2 F3: 이 표가 굴진방향 거리 눈금("굴진방향→ 100m 200m …")
                    # 이나 사유 딸린 평가 줄("부적정 (사유: …)")과 가까우면(같은
                    # 도식 세트) "보통"·"불량"이 다 있는 온전한 범례라도 여전히
                    # 수량산출 예시도(사례1/사례2)다 — ND10* 콘크리트관 추진 그룹
                    # 재검(코치 09-17): 강관압입공 예시도(도식 표 밖 거리표시)와
                    # 같은 규칙으로, "표처럼 보여도 도식"이면 뺀다. 이런 맥락 신호가
                    # 전혀 없는 범례표(코드+거리구간+등급만 있고 눈금·평가문 없음)는
                    # 여전히 실제 예시 표로 남긴다(기존 gold_dev 사례 보존).
                    near_diagram_ctx = False
                    probe = [s for s in spans if tb[1] - 60 <= s.yc <= tb[3] + 90]
                    for _ly, ln in _cluster_lines_y(probe, gap=6.0):
                        lnt = ln.strip()
                        if DIAGRAM_ARROW_RE.match(lnt) or DIAGRAM_EVAL_REASON_RE.match(
                            lnt.replace(" ", "")
                        ):
                            near_diagram_ctx = True
                            break
                    is_full_quality_range = (
                        is_quality_header
                        and "보통" in row0
                        and "불량" in row0
                        and not near_diagram_ctx
                    )
                    if is_quality_header and not is_full_quality_range:
                        figure_boxes.append(tb)
                        continue
                    # N2: 그림·도식 상자(이미지 겹침·비교대괄호·단가없는 거리범례)는
                    # 주석 표로 넣지 않는다 — 다만 (a)의 온전한 범례표는 코드+거리
                    # 구간이 있어도 예시 데이터이므로 이 그림 판정에서 제외한다.
                    if not is_full_quality_range and _is_diagram_table(
                        tb, full_txt, t.row_count, image_boxes
                    ):
                        figure_boxes.append(tb)
                        continue
                    flat = _flatten_subtable(t, words_all)
                    if flat:
                        table_entries.append((tb[1], flat))
                # F2(wrong_notes 2024H2#p177#233): find_tables() 가 놓친 작은
                # 소표를 격자선에서 직접 읽어 보강한다(위 tables 루프가 이미
                # 처리한 상자·그림 상자와 겹치면 _geom_note_subtables 자체가
                # 건너뛴다).
                claimed_boxes = [t.bbox for t in tables] + figure_boxes
                for gy, gflat, gbox in _geom_note_subtables(
                    hx0, hx1, y0, y1, hlines, vlines, words_all, claimed_boxes
                ):
                    table_entries.append((gy, gflat))
                    figure_boxes.append(gbox)
                have_sub = bool(table_entries)
                # G02i2 2차(T09 인접 결함, 코치 재검 2024H1 p45#107 "ED*** 거푸집"
                # ② 항목): find_tables() 로 칸을 뽑아 이어붙인 have_sub_blob 은
                # 줄바꿈 지점의 쉼표를 이따금 빠뜨린다(칸 안에서 "옹벽,"→"파라펫트,"
                # 로 줄바뀜한 자리가 "옹벽 파라펫트,"로 붙어 나온다) — 반면 같은
                # 내용을 낱말 단위로 그대로 읽은 줄(raw_lines_y)은 "옹벽," 그대로다.
                # 쉼표 하나 차이로 포함 판정이 깨지면 표 첫 칸과 완전히 같은 내용의
                # 줄이 별도 "기호 없는 항목"(고아)으로 새로 생겨, F1 이어붙임이 그걸
                # 엉뚱하게 앞 항목 문장에 붙여 넣었다(실제로는 표 칸의 중복일 뿐,
                # 앞 항목의 이어짐이 아니다). 쉼표도 구분자로 보고 없앤 뒤 비교한다.
                have_sub_blob = re.sub(r"[\s|/,]+", "", "".join(t for _, t in table_entries))
                band = [s for s in spans if y0 < s.yc < y1]
                if figure_boxes:
                    band = [
                        s
                        for s in band
                        if not any(
                            fb[0] - 1 <= s.xc <= fb[2] + 1 and fb[1] - 1 <= s.yc <= fb[3] + 1
                            for fb in figure_boxes
                        )
                    ]
                raw_lines_y = _cluster_lines_y(band, gap=6.0)
                # text lines
                text_entries: list[tuple[float, str]] = []
                for ly, ln in raw_lines_y:
                    s = ln.strip()
                    if not s:
                        continue
                    if "■" in s[:3]:
                        continue
                    if CODE_RE.match(s.split()[0] if s.split() else ""):
                        continue
                    if s in HEADER_MAP or s.replace(" ", "") in HEADER_MAP:
                        continue
                    # N1 회귀: 예전엔 s.startswith("단가") 처럼 접두 일치로 걸렀는데,
                    # "단가로서 할증을 포함…" 같은 정상 주석 문장까지 통째로 지워졌다
                    # (2025H2 p27 AB41* 그룹 ② 항목 등). 표 머리글 줄은 여러 칸이 한
                    # 줄로 뭉쳐 나오므로(예: "공종코드 공종명칭 규격 단위 단가 노무비율"),
                    # 공백을 뺀 줄 전체가 머리글 낱말들만의 연속으로 완전히 채워질 때만
                    # 머리글 줄로 본다(접두 일치가 아니라 전체 일치).
                    if _HEADER_ROW_RE.fullmatch(s.replace(" ", "")):
                        continue
                    # N1: 여기서는 확실한 구조적 잡음만 거른다. "짧고 한글 3연속
                    # 없음" 같은 약한 판정은 _note_items() 가 buf 상태를 보고
                    # 한다 — 여기서 먼저 걸러내면 줄바꿈 끝 조각이 통째로
                    # 사라진다(N1 결함의 원인이었다).
                    if _is_structural_note_junk(s):
                        continue
                    # (표) 글줄 중복: 이미 flatten 한 표에 들어 있는 글만 건너뜀
                    # ("|"·"/" 칸·행 구분자 때문에 이어져 있던 낱말이 끊겨 보여
                    # 포함 판정을 놓치지 않도록 그 구분자도 지우고 비교한다)
                    if have_sub and s[0] not in CIRCLED and not s.startswith("(표)") and not s.startswith("<"):
                        if re.sub(r"[\s,]+", "", s) in have_sub_blob:
                            continue
                        if re.sub(r"\s+", "", s).startswith("구분"):
                            continue
                        if re.search(r"매끈한마감|보통마감|거친마감", s):
                            continue
                    text_entries.append((ly, s))
                # N2/N4: 문서상 실제 위치(y) 순서로 표·글줄을 다시 섞는다 — 표를
                # 항상 앞세우던 예전 방식은 "④…아래와 같다." 뒤에 오는 표가
                # 엉뚱하게 ③ 앞에 표시되는 순서 뒤바뀜을 냈다.
                lines_txt = [t for _, t in sorted(table_entries + text_entries, key=lambda e: e[0])]
                word_caps = _figure_captions_from_words(words_all, hx0, hx1, y0, y1)
                kept_lines: list[str] = []
                span_caps: list[str] = []
                for s in lines_txt:
                    if _is_figure_caption_line(s):
                        span_caps.append(_collapse(s))
                    else:
                        kept_lines.append(s)
                caps = word_caps or span_caps
                if caps:
                    grp.setdefault("figures", [])
                    for cap in caps:
                        grp["figures"].append({"caption": cap, "pdf_page": pno})
                grp["notes"].extend(_note_items(kept_lines, pno))

            # 쪽 넘김으로 그룹을 끝내지 않음: 첫머리 주석(⑤부터, 【단가정의】 없이 ①)을 이전 그룹에
            first_group_y = min((e[0] for e in events if e[1] == "group"), default=None)
            first_table_y = min((e[0] for e in events if e[1] == "table"), default=None)
            first_notes_y = min((e[0] for e in events if e[1] == "notes"), default=None)
            # G02i2 F2(a): first_table_y 는 이 표 첫 코드의 y중심이라, 명칭 칸이
            # 두 줄로 접혀 코드보다 위쪽에 인쇄되면(예: "샌드위치패널 설치/\n벽체")
            # 그 첫 줄이 코드 y중심보다 위(작은 y)에 있어 앞 그룹 주석 밴드
            # (8~top_limit)에 새어 들어간다(review G02i2: 2024H1 p113→114
            # "OJ*****" 등). 코드 y중심이 아니라 표 위쪽 테두리(헤더 밑 가로선)
            # 에서 끊는다 — 그 선은 어떤 칸이 몇 줄로 접히든 항상 표 시작보다
            # 위에 있다.
            if first_table_y is not None:
                top_hlines = [
                    h
                    for h in _wide_h(hlines, first_table_y - 40, first_table_y + 2, min_w * 0.5)
                    if h.a < hx1 - 20 and h.b > hx0 + 20
                ]
                if top_hlines:
                    first_table_y = min(first_table_y, max(h.c for h in top_hlines))
            top_limit = page.rect.height
            for y in (first_group_y, first_table_y):
                if y is not None:
                    top_limit = min(top_limit, y)
            # G02i2 F2(b): 다음 ■ 머리글 한 줄 안에서도 글자마다(폰트가 다르면)
            # yc 가 소수점 단위로 어긋난다 — first_group_y 는 그 중 한 낱말("■")의
            # yc 라서, 바로 이 값을 경계(<top_limit)로 쓰면 같은 줄의 다른
            # 낱말("BA*****,"·"BB*****" 등, 코드값이라 항목 기호처럼 안 보여
            # 그대로 이어붙는다)이 yc 가 살짝만 작아도 새어 든다(review G02i2:
            # 2024H1 p119→120 "BA*****, BB***** 강관" ⑦ 등). 아래 next_y 계산과
            # 같은 여유(1.5)를 여기도 둬 머리글 줄 전체를 확실히 뺀다.
            if first_group_y is not None and first_group_y == top_limit:
                top_limit -= 1.5
            has_early_notes = first_notes_y is not None and first_notes_y < top_limit - 1
            if last_group is not None and not has_early_notes and top_limit > 40:
                attach_notes_from(8.0, top_limit, last_group)

            for i_ev, (ey, etype, payload) in enumerate(events):
                if i_ev + 1 < len(events):
                    next_y = events[i_ev + 1][0]
                    if events[i_ev + 1][1] == "group":
                        # N1 회귀 방지: 다음 ■ 머리글 줄 안에서도 글자마다(폰트가
                        # 다르면) yc 가 소수점 단위로 어긋난다(예: "■"·"JG******"
                        # 는 190.752, "금속덮개"·"뚜껑"은 191.094). group 이벤트의
                        # ey 는 그 중 한 낱말(대개 "■")의 yc 라서, 바로 앞 주석
                        # band 의 경계(<next_y)가 그보다 살짝 작은 값을 가진 같은
                        # 줄의 다른 낱말("JG******","(",")")만 걸러내고 새어
                        # 들어가게 했다(review_round1 실측: 2025H1 p103~104
                        # "JG****** ( )" 조각). 여유를 둬 같은 줄 전체를 확실히
                        # 뺀다.
                        next_y -= 1.5
                else:
                    next_y = page.rect.height - 12
                if etype == "banner":
                    letter, name = payload  # type: ignore[misc]
                    last_major = letter
                    last_major_name = name
                elif etype == "group":
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
                    real_vxs = list(vxs)
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

                    # H1: 이 표 뭉치가 같은 그룹 안에서 쪽·단만 넘어 이어지는 것이면
                    # 직전 뭉치의 소제목·명칭을 이어받는다(그룹이 바뀌면 new_group 이 끊음).
                    current_sub = last_sub  # (pattern, text)
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

                    prev_name = last_prev_name
                    for (kind, sp), (y0, y1) in zip(items_sorted, bands):
                        if kind == "star":
                            pat = sp.text.strip()
                            # 소제목 텍스트: 코드 열 오른쪽 전체
                            txt = _collapse(_cell_text(words_all, chars, name_col[0], tx, y0, y1, real_vxs))
                            current_sub = (pat, txt)
                            subheaders.append(
                                {
                                    "half": half,
                                    "pdf_page": pno,
                                    "page_half": ph,
                                    "y0": round(y0, 1),
                                    "code_pattern": pat,
                                    "text": txt,
                                    "group_id": grp["group_id"] if grp else None,
                                }
                            )
                            continue

                        code = sp.text.strip()
                        name_raw = _cell_text(words_all, chars, name_col[0], name_col[1], y0, y1, real_vxs)
                        spec_raw = _cell_text(words_all, chars, spec_col[0], spec_col[1], y0, y1, real_vxs)
                        unit_raw = _cell_text(words_all, chars, unit_col[0], unit_col[1], y0, y1, real_vxs)
                        price_raw0 = _cell_text(words_all, chars, price_col[0], price_col[1], y0, y1, real_vxs)
                        labor_raw0 = _cell_text(words_all, chars, labor_col[0], labor_col[1], y0, y1, real_vxs)
                        remark_raw = (
                            _cell_text(words_all, chars, remark_col[0], remark_col[1], y0, y1, real_vxs)
                            if has_remark
                            else ""
                        )

                        # 코드 열이 명칭 첫 글자를 삼킨 경우(맹암거, PHC, L형…) 되돌림
                        gap_txt = _cell_text(words_all, chars, code_col[0], name_col[0], y0, y1, real_vxs)
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
                                if LABOR_RE.match(t) or _is_half_labor(t):
                                    labor_tok = s.text.strip()
                                    break
                        if not labor_tok:
                            labor_tok = _collapse(labor_raw0)

                        price_raw, price, status = _parse_price(price_tok or price_raw0)
                        labor_raw, labor_ratio = _parse_labor(labor_tok)
                        # 단가 칸이 숫자가 아니고 노무비율이 ‘YY상/하 이면 폐지 레코드
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

                        # 명칭 칸이 상속 표시(＂ 등)이거나, 원문에 상속 표시조차 없이
                        # 통째로 비어 있는 경우(표 인쇄 시 표시를 누락한 실제 사례,
                        # 예: 2024H1 p68 NA109.22202)도 직전 명칭을 이어받는다.
                        name_blank = not re.sub(r"\s+", "", name_raw or "")
                        inherited = _is_inherit(name_raw) or name_blank
                        name_group = current_sub[0] if current_sub else None
                        if inherited:
                            name = current_sub[1] if current_sub else prev_name
                            if not name:
                                warnings.append(
                                    f"명칭 상속 실패(이어받을 명칭 없음): code={code} "
                                    f"half={half} pdf_page={pno} name_raw={name_raw!r}"
                                )
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

                    # H1: 이 표 뭉치가 끝나며 도달한 소제목·명칭 상태를 다음 뭉치(쪽·단이
                    # 바뀌어도)로 넘긴다. 그룹이 바뀌면 new_group() 이 다시 끊는다.
                    last_sub = current_sub
                    last_prev_name = prev_name

                    # N1: 【단가정의】 라벨 자체가 원문에서 빠진 채(인쇄 누락) 표
                    # 바로 뒤에 "①…"로 곧장 시작하는 주석이 있으면, "notes" 이벤트가
                    # 아예 안 생겨 이 구간이 통째로 안 읽힌다(review_round1 실측:
                    # 2024H1 p86 EE***** 무수축 모르타르 타설 — 4권 전체에서 재현).
                    # 이 표 뭉치가 끝난 자리~다음 이벤트 사이에 【단가정의】 이벤트가
                    # 없고, 첫 글줄이 원문자·※로 시작하면 라벨 없는 주석으로 보고
                    # 그대로 이 그룹에 붙인다(맨 위 페이지 이어받기와 같은 원리,
                    # 다만 여기는 쪽 중간 표 뒤).
                    has_notes_ev = any(
                        et == "notes" and y_max + 1 < ey2 < next_y for ey2, et, _ in events
                    )
                    if not has_notes_ev and next_y - y_max > 10:
                        peek = [s for s in spans if y_max + 1 < s.yc < next_y]
                        peek_lines = _cluster_lines_y(peek, gap=6.0)
                        # 표 마지막 행의 규격 등이 다음 줄로 넘어간 잔재("(철골기둥
                        # 하부)" 같은 줄바꿈된 칸)가 note 시작보다 먼저 나올 수
                        # 있어, 첫 줄만 보지 않고 앞 몇 줄 안에서 원문자·※ 시작
                        # 줄을 찾는다(너무 멀리까지 찾으면 엉뚱한 뒤쪽 내용을
                        # 끌어올 위험이 있어 3줄로 제한한다).
                        note_start_y = None
                        for ly, ln in peek_lines[:3]:
                            s = ln.strip()
                            if s and (s[0] in CIRCLED or s.startswith("※")):
                                note_start_y = ly
                                break
                        if note_start_y is not None:
                            attach_notes_from(
                                note_start_y - 1, _header_row_trim(note_start_y, next_y, headers), grp
                            )

                elif etype == "notes":
                    grp = current_group or last_group
                    attach_notes_from(ey - 2, _header_row_trim(ey, next_y, headers), grp)

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

    # G02i2 F1: 쪽·단 경계에서 잘려 항목 기호 없이 남은 이어짐 항목을, 그룹의
    # notes 가 문서 전체에 걸쳐 다 모인 뒤 한 번에 합친다(그룹별로 페이지 진행
    # 순서를 그대로 유지하는 groups 리스트 자체 순서를 바꾸지 않는다).
    for g in groups:
        if g.get("notes"):
            g["notes"] = _merge_note_continuations(g["notes"])

    doc.close()
    return {
        "records": records,
        "groups": groups,
        "subheaders": subheaders,
        "pages": pages_out,
        "sha256": sha,
        "half": half,
        "warnings": warnings,
    }
