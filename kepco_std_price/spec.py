"""한전 표준시장단가 추출기·게이트가 공유하는 상수.

게이트는 이 모듈의 상수(및 순수 기하 헬퍼)만 쓰고 extract.py / extract_hwp.py
의 함수를 부르지 않는다. 정규식·임계값·단위표를 한곳에 둔다(G02j L7).
"""
from __future__ import annotations

import re

# --- 공종코드 ---
CODE_FIND = re.compile(r"[A-Z]{2}\d{3}\.\d{5}")
CODE_RE = CODE_FIND
CODE_FULL = re.compile(r"^[A-Z]{2}\d{3}\.\d{5}$")
STAR_FIND = re.compile(r"[A-Z]{2}\d{3}\.\d+\*")
STAR_RE = STAR_FIND

# 단가: 천단위 쉼표 정수 또는 순수 정수.
# 소수·범위·단위가 붙은 값(1,234.5 / 1,000～2,000 / 500원)은 이 패턴 밖이며
# 행을 드롭하지 않고 price=null + price_raw 보존(G02j L2, price_unparsed).
PRICE_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$|^\d+$")
LABOR_RE = re.compile(r"^\d+(?:\.\d+)?%$")

# L1: 폐지 반기 표기(‘25상)가 아래 아포스트로피로 인쇄돼도 잡는다.
# U+2018 LEFT SINGLE ‘ / U+0027 APOSTROPHE ' / U+2032 PRIME ′ /
# U+0060 GRAVE ` / U+2019 RIGHT SINGLE ’ / U+FF07 FULLWIDTH ＇
HALF_LABOR_APOS = "\u2018'\u2032`\u2019\uff07"
HALF_LABOR_RE = re.compile(r"^[" + HALF_LABOR_APOS + r"]?\d{2}[상하]")

HALF_FMT_RE = re.compile(r"^20\d{2}H[12]$")

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
CIRCLED_CHARS = CIRCLED

GROUP_MARK = "■"
BANNER_PREFIX = "대분류"
# L6: 합쳐진 배너 글자 수 제한 없음(대분류 Q, R, S …). major 는 첫 글자.
BANNER_RE = re.compile(r"대분류\s*([A-Z])(?:\s*[,，]\s*[A-Z])*\s*(.*)$")
BANNER_TRAIL_RE = re.compile(r"[·.…⋯]+.*$")

# L10: 1행짜리 작은 표는 헤더/데이터 세로선이 ~19.8pt 로 끊긴다.
# 단가칸 상자 세로선은 ~8.5pt(가로 2단) / ~12pt(세로). 15 아래로 내리면 잡음.
# (G02d 실측; conservation._vlines / extract._page_lines 동일)
VLINE_MIN_LEN_PT = 15.0

# L10: 대분류명 — 글자 사이 공백·목차 점선·쪽번호를 뺀 뒤 한글 2자 이상.
# 24자를 넘으면 표 잔여/다음 줄이 붙은 것으로 보고 버린다. (G02f 실측)
MAJOR_NAME_MAX_LEN = 24

FIGURE_CAPTION_PREFIXES = ("[그림", "[표준도]")

# PDF 분야 표지(사이에 반기 문구가 끼지 않은 이어붙임)
FIELD_RE_PDF = re.compile(r"([가-힣]+(?:및[가-힣]+)*)분야자체표준시장단가")
# HWP 분야 줄은 '자체표준시장단가' 가 선택
FIELD_RE_HWP = re.compile(r"([가-힣]+(?:및[가-힣]+)*)분야(?:자체표준시장단가)?")

# G02i2 F4: 실물 렌더로 확인한 PUA 만. 확인 안 된 코드는 넣지 않는다.
PUA_DIGIT_MAP = {
    "\uE034": "1",
    "\uE035": "2",
    "\uE037": "4",
    "\uE038": "5",
    "\uE039": "6",
    "\uE03D": "0",
}
PUA_POINT_MAP = {"\uE053": "."}
PUA_SUPER_MAP = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
}

# 가로 2단 경계 탐색 띠(페이지 폭 비율). 뚜렷한 세로선·낱말 공백이 없으면 정중앙.
LANDSCAPE_GUTTER_BAND = (0.35, 0.65)
# 정중앙과 이보다 가까우면 폴백(기존 4권 대칭 2단 유지).
# 표 내부 세로선·칸 사이 공백은 단 경계가 아니다(실측: 가로 2단 표 세로선이
# 중앙에서 80~100pt 어긋난 위치에 있다). 이보다 크게 치우친 거터만 채택.
LANDSCAPE_GUTTER_CENTER_TOL_PT = 120.0
LANDSCAPE_WORD_GAP_MIN_PT = 18.0

# 수량산출 예시도 거리구간(0-150m, 50m~100m, L=70m～100m)
DIST_RANGE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*m\s*[~∼\-～]\s*\d+(?:\.\d+)?\s*m"
    r"|L\s*=\s*\d+(?:\.\d+)?\s*m"
    r"|\d+(?:\.\d+)?\s*[~∼\-～]\s*\d+(?:\.\d+)?\s*m"
)


def cluster_floats(xs: list[float], tol: float) -> list[float]:
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


def landscape_gutter_x(
    width: float,
    v_xs: list[float],
    word_xs: list[float] | None = None,
) -> float:
    """가로 판형 단 경계 x. 세로선·낱말 x 군집, 없으면 정중앙.

    추출 함수가 아니다. extract 와 conservation 이 같은 임계값으로 같은 값을 낸다.
    """
    mid = width / 2.0
    if width <= 0:
        return mid
    lo = width * LANDSCAPE_GUTTER_BAND[0]
    hi = width * LANDSCAPE_GUTTER_BAND[1]
    found: float | None = None
    band_v = [x for x in v_xs if lo <= x <= hi]
    if len(band_v) >= 2:
        clusters = cluster_floats(band_v, 8.0)
        if clusters:
            found = min(clusters, key=lambda x: abs(x - mid))
    if found is None and word_xs:
        xs = sorted(x for x in word_xs if lo <= x <= hi)
        best_gap = 0.0
        best_at: float | None = None
        for i in range(len(xs) - 1):
            gap = xs[i + 1] - xs[i]
            if gap > best_gap:
                best_gap = gap
                best_at = (xs[i] + xs[i + 1]) / 2.0
        if best_at is not None and best_gap >= LANDSCAPE_WORD_GAP_MIN_PT:
            found = best_at
    if found is None:
        return mid
    if abs(found - mid) <= LANDSCAPE_GUTTER_CENTER_TOL_PT:
        return mid
    # 표 칸 세로선은 근처에 낱말이 붙어 있다. 단 경계는 비어 있다.
    if word_xs:
        dens = sum(1 for w in word_xs if abs(w - found) <= 15.0)
        if dens > 3:
            return mid
    return found
