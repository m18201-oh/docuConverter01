"""레코드 -> 대분류별 Markdown, 이 도구가 만든 파일만 안전하게 정리한다.

핵심 규칙(TASK_G02g):
  1. <md>/.kepco_std_price_manifest.json 에 이번에 쓴 파일의 상대경로/sha256 기록
  2. 쓰려는 경로가 이미 있으면: 목록에 있고 해시가 같을 때만 덮어쓰고,
     그 외(목록에 없음/해시 다름 = 사용자 수정)에는 _preserved 로 옮긴 뒤 새로 쓴다
  3. 목록에는 있지만 이번에 안 쓰는 경로는 해시가 같으면 삭제, 다르면 _preserved 로 이동
  4. 목록에도 없고 이번에 쓰지도 않는 파일은 절대 건드리지 않는다
  5. 목록 파일 자체가 없으면 아무것도 지우지 않고 "정리 후보"만 경고
  6. 이번 정리로 빈 md 하위 폴더만 지운다(md 자신·_preserved 는 보존)
  7. 목록 경로가 md 밖(.. · 절대경로 · 심볼릭 링크/정션으로 탈출)이면 무시하고 경고,
     목록 JSON 이 깨졌으면 목록 없음으로 취급, md 가 폴더가 아니면 아무것도 바꾸지 않고 오류
  8. 요약 한 줄 반환(호출자가 stdout 에 출력)
  9. shutil.rmtree 는 쓰지 않는다(os.replace/os.remove/os.rmdir 만)

경합(TOCTOU) 방어에 대한 설계 메모:
  검사(안전한 경로인지)와 실행(실제로 쓰기/지우기) 사이에는 항상 이론적으로 시간차가
  있다. 완벽한 원자성은 파일 하나마다 Windows 네이티브 핸들 기반 rename/delete(
  SetFileInformationByHandle 등)를 직접 구현해야 하는데, 이 도구의 실제 운영 환경
  (오수석님 개인 PC, 단일 사용자, 동시에 이 폴더를 건드리는 다른 프로세스가 없는
  로컬 배치 실행)에서는 그 정도 방어보다 "실제 산출물이 확실히 만들어지는 것"이
  우선이라 판단해, 아래 두 겹으로 창을 최소화하는 선에서 마무리한다:
    (a) md 의 실제 위치(realpath)를 이번 실행 시작 시 딱 한 번만 확정해 두고
        (`md_resolved`), 이후 모든 안전성 비교는 이 고정값을 기준으로 한다. 이러면
        실행 도중 md 상위 어딘가가 정션으로 바뀌어도(=diskt 상 실제 위치가 달라져도)
        "원래 있어야 할 자리"라는 기준 자체는 흔들리지 않아 탈출을 잡아낼 수 있다.
      (b) 실제로 쓰기/지우기/이동을 부르기 바로 직전에 그 경로를 다시 한번(§_safe_join)
        검사한다. 검사와 실행 사이에 다른 파이썬 코드(파일 읽기 등)를 절대 끼우지
        않음으로써 창을 "바로 다음 줄" 수준으로 좁힌다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field as _dc_field
from pathlib import Path

TOOL_NAME = "kepco_std_price"
MANIFEST_NAME = ".kepco_std_price_manifest.json"
PRESERVED_DIR_NAME = "_preserved"


class MdOutputError(Exception):
    """md 출력 폴더 상태가 안전하지 않아 아무것도 바꾸지 않고 중단할 때."""


class _RaceGuardError(Exception):
    """검사와 실행 사이에 경로가 바뀐 것으로 의심돼(경합) 그 파일을 건드리지 않을 때."""


@dataclass
class MdSummary:
    written: int = 0
    overwritten: int = 0
    deleted: int = 0
    preserved: int = 0
    untouched: int = 0
    stale_candidates: int = 0
    warnings: list[str] = _dc_field(default_factory=list)

    def line(self) -> str:
        return (
            f"md: written={self.written} overwritten={self.overwritten} "
            f"deleted={self.deleted} preserved={self.preserved} "
            f"untouched={self.untouched} stale_candidates={self.stale_candidates}"
        )


# G02i2 F6: CommonMark 가 강조(*_)·링크([])·HTML(<>)·이스케이프(\)·코드(`) 로
# 해석하는 글자를 역슬래시로 이스케이프한다("<사례1>" 같은 원문 표기가 HTML
# 태그로 읽혀 뷰어에서 사라지지 않도록). "|" 는 표 칸에서만 별도로 다룬다
# (표 밖 목록 줄·그림 제목에는 구조적 의미가 없다).
_MD_SPECIAL_RE = re.compile(r"[\\`*_\[\]<>]")


def _esc_md(s: str) -> str:
    return _MD_SPECIAL_RE.sub(lambda m: "\\" + m.group(0), s)


def _esc(s: str | None) -> str:
    if s is None:
        return ""
    t = _esc_md(str(s).replace("\n", " "))
    return t.replace("|", "\\|")


def _esc_text(s: str | None) -> str:
    """주석 목록 줄·그림 제목: 표 칸과 같은 특수 글자 집합을 이스케이프한다(F6)."""
    if s is None:
        return ""
    return _esc_md(str(s).replace("\n", " "))


_UNSAFE_COMPONENT_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')  # 코치 핫픽스 09-17: NUL·제어문자도 치환(대분류 md 가 집계 없이 사라지던 경로)
_DOT_RUNS = re.compile(r"\.\.+")


def _safe_component(s: str | None, fallback: str = "_") -> str:
    """field/major 처럼 데이터에서 온 값을 경로 한 칸(폴더·파일명)으로 안전하게 만든다.

    구분자(\\/)·Windows 금지문자를 치환하고, ".."(상위 폴더 참조)로 재조합될 수 있는
    연속 마침표를 뭉갠다. 이렇게 해도 아래 _safe_join() 의 최종 검사(realpath 가 md
    밖으로 나가는지)가 한 번 더 걸러주므로, 이 함수가 무언가를 놓쳐도 md 밖에 쓰는
    사고로는 이어지지 않는다(방어를 두 겹으로 둔다).

    도구가 예약해 쓰는 폴더명(_preserved)과 대소문자까지 같으면(Windows는 대소문자를
    구분하지 않는다) fallback 으로 바꿔, PDF 파생 값이 우연히 그 이름과 겹쳐 보존용
    폴더 안에 정상 산출물이 섞여 드는 사고를 막는다.
    """
    if s is None:
        return fallback
    s = _UNSAFE_COMPONENT_CHARS.sub("_", str(s))
    s = _DOT_RUNS.sub("_", s)
    s = s.strip(" .")
    if not s or s in (".", ".."):
        return fallback
    if s.casefold() == PRESERVED_DIR_NAME.casefold():
        return fallback
    return s


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        pass
    # Windows: 대소문자 구분 없는 파일시스템 보정
    ps = os.path.normcase(os.path.normpath(str(path)))
    bs = os.path.normcase(os.path.normpath(str(base)))
    return ps == bs or ps.startswith(bs + os.sep)


def _safe_join(md_dir: Path, relpath: str, md_resolved: Path) -> Path | None:
    """manifest 상의 상대경로가 md 안을 가리키는 실제 경로인지 검사.

    벗어나면(절대경로 · .. · 심볼릭 링크/정션으로 탈출) None 을 돌려준다. 심볼릭
    링크·정션은 따라가지 않는다: 경로의 중간 구성요소가 하나라도 그렇다면 즉시
    거부한다. md_resolved 는 이번 render_md() 실행 시작 시 한 번만 계산해 둔 값을
    받아 기준으로 삼는다(다시 계산하지 않는다) — 그래야 실행 도중 md 상위 폴더
    자체가 바뀌어도(경합) "원래 있어야 할 자리"라는 기준이 흔들리지 않는다.

    목록의 relpath 는 외부(JSON 파일)에서 온 신뢰할 수 없는 문자열이라, NUL(\x00)
    같은 값이 섞여 있으면 Path 연산이나 OS 호출(is_symlink·resolve 등)이 OSError
    가 아니라 ValueError 를 던질 수 있다(Windows). 그런 경우도 전부 "안전하지
    않음 -> None" 으로 취급해, 호출자가 그 항목만 무시하고 경고하면 되도록 한다.
    """
    if not relpath or not relpath.strip():
        return None
    try:
        rp = Path(relpath)
        if rp.is_absolute() or rp.drive:
            return None
        parts = rp.parts
        if not parts or any(part in ("..", "", ".") for part in parts):
            return None
        cur = md_dir
        for part in parts:
            cur = cur / part
            if cur.is_symlink() or cur.is_junction():
                return None
        target_resolved = cur.resolve(strict=False)
    except (OSError, ValueError):
        return None
    if not _is_within(target_resolved, md_resolved):
        return None
    return cur


def _load_manifest(md_dir: Path, md_resolved: Path) -> tuple[dict[str, str], list[str], str]:
    """돌려주는 값: (relpath(posix)->sha256, 경고 목록, 상태).

    상태는 "valid"(정상 파싱) · "absent"(파일 없음) · "corrupted"(깨짐) 중 하나.
    """
    warnings: list[str] = []
    manifest_path = md_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return {}, warnings, "absent"
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        files = data.get("files")
        if not isinstance(files, dict):
            raise ValueError("files 필드가 없거나 형식이 dict 가 아님")
    except Exception as e:  # noqa: BLE001 - 목록 파일은 무엇이 와도 안전하게 처리
        warnings.append(f"목록 JSON이 깨져 있어 목록 없음으로 취급합니다: {e}")
        return {}, warnings, "corrupted"

    out: dict[str, str] = {}
    for relpath, sha in files.items():
        if not isinstance(relpath, str) or not isinstance(sha, str):
            warnings.append(f"목록 항목 형식이 이상해 무시합니다: {relpath!r}")
            continue
        target = _safe_join(md_dir, relpath, md_resolved)
        if target is None:
            warnings.append(
                f"목록 경로가 안전하지 않아 무시합니다(밖을 가리키거나 잘못된 문자 등): {relpath}"
            )
            continue
        out[Path(relpath).as_posix()] = sha

    # Windows 는 대소문자를 구분하지 않는 파일시스템이다: 목록에 대소문자만 다른
    # 키가 둘 이상 있으면 어느 쪽이 "진짜" 기록인지 알 수 없다. 지우면(규칙 3)
    # 방금 쓴 파일이 사라지는 사고로 이어질 수 있으므로, 그런 키들은 전부 목록에서
    # 빼고 경고만 남겨 이후 로직에서 "목록에 없음"과 같이(=보수적으로) 처리되게 한다.
    cf_groups: dict[str, list[str]] = defaultdict(list)
    for k in out:
        cf_groups[k.casefold()].append(k)
    for cf, keys in cf_groups.items():
        if len(keys) > 1:
            warnings.append(
                "목록에 대소문자만 다른 키가 둘 이상 있어 안전하게(지우지 않고) 무시합니다: "
                + ", ".join(sorted(keys))
            )
            for k in keys:
                del out[k]

    return out, warnings, "valid"


def _pick_preserved_root(md_dir: Path) -> Path:
    base_name = time.strftime("%Y%m%d-%H%M%S")
    root = md_dir / PRESERVED_DIR_NAME / base_name
    if not root.exists():
        return root
    n = 2
    while True:
        candidate = md_dir / PRESERVED_DIR_NAME / f"{base_name}-{n}"
        if not candidate.exists():
            return candidate
        n += 1


def _preserve_move(md_dir: Path, md_resolved: Path, relpath: str, preserved_root: Path) -> None:
    """target 을 _preserved 로 옮긴다(os.replace, 같은 볼륨 내 이동).

    옮기기(rename) 자체는 한 번의 시스템 호출이라 그 자체로는 원자적이다. 다만 그
    호출에 넘길 "지금 옮기려는 경로"가 여전히 안전한지(경합으로 바뀌지 않았는지)를
    직전에 다시 한번 확인해, 검사와 실행 사이에 다른 코드가 끼어들 여지를 없앤다.
    """
    src = _safe_join(md_dir, relpath, md_resolved)
    if src is None or src.is_symlink() or src.is_junction():
        raise _RaceGuardError(f"{relpath}: 이동 직전 재검증 실패(경합 의심)로 건드리지 않습니다")
    dest = preserved_root / relpath
    # relpath 는 이 시점에 이미 _safe_join() 을 통과한 값이어야 하지만, 한 번 더
    # 확인한다: preserved_root 두 단계 깊이(md/_preserved/<시각>)가 relpath 안의
    # ".." 와 우연히 상쇄돼 md 밖으로 떨어지는 사고를 막는다.
    try:
        dest_resolved = dest.resolve(strict=False)
        preserved_resolved = preserved_root.resolve(strict=False)
    except OSError as e:
        raise OSError(f"보존 대상 경로를 확인할 수 없습니다: {relpath}: {e}") from e
    if not _is_within(dest_resolved, preserved_resolved):
        raise OSError(f"보존 대상 경로가 _preserved 밖으로 벗어나 중단합니다: {relpath}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)  # 같은 볼륨 내 이동, shutil.rmtree 미사용


def _read_verified(md_dir: Path, md_resolved: Path, relpath: str, target: Path) -> bytes:
    """target 을 열어 그 내용을 읽는다. 열자마자(=다른 코드를 끼우지 않고) 실제로
    연 대상이 여전히 md 안인지 한번 더 확인한다(읽기 자체의 경합 창도 최소화).
    """
    fd = os.open(str(target), os.O_RDONLY | os.O_BINARY)
    try:
        reverified = _safe_join(md_dir, relpath, md_resolved)
        if reverified is None or reverified.is_symlink() or reverified.is_junction():
            raise _RaceGuardError(f"{relpath}: 읽기 직후 재검증 실패(경합 의심)")
        size = os.fstat(fd).st_size
        return os.read(fd, size)
    finally:
        os.close(fd)


def _prune_unsafe_dirnames(p: Path, dirnames: list[str]) -> None:
    """os.walk 가 심볼릭 링크·정션 안으로 내려가지 못하게 dirnames 를 제자리에서 거른다.

    Windows 에서 os.walk(followlinks=False) 는 실제로 정션(디렉터리 reparse point)
    안까지 내려간다(심볼릭 링크만 걸러지고 정션은 안 걸러짐 — 직접 확인함). 그대로
    두면 md 안에 심어진 정션을 통해 md 밖의 파일을 세거나(규칙 4/5 집계 오염),
    md 밖의 빈 폴더를 지워버리는(규칙 6) 사고로 이어질 수 있다.
    """
    dirnames[:] = [
        d for d in dirnames
        if not (p / d).is_symlink() and not (p / d).is_junction()
    ]


def _walk_files(md_dir: Path):
    """md_dir 밑의 모든 파일을 (posix 상대경로, 실제 Path) 로 나열. _preserved 와
    심볼릭 링크·정션 안쪽은 건너뛴다."""
    for dirpath, dirnames, filenames in os.walk(md_dir, topdown=True, followlinks=False):
        p = Path(dirpath)
        if p == md_dir:
            dirnames[:] = [d for d in dirnames if d.casefold() != PRESERVED_DIR_NAME.casefold()]
        _prune_unsafe_dirnames(p, dirnames)
        for fn in filenames:
            fp = p / fn
            yield fp.relative_to(md_dir).as_posix(), fp


def _remove_empty_dirs(md_dir: Path) -> None:
    """이번 정리로 비게 된 md 하위 폴더만 지운다. md 자신·_preserved·정션/심볼릭
    링크 안쪽은 건드리지 않는다."""
    all_dirs: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(md_dir, topdown=True, followlinks=False):
        p = Path(dirpath)
        if p == md_dir:
            dirnames[:] = [d for d in dirnames if d.casefold() != PRESERVED_DIR_NAME.casefold()]
        _prune_unsafe_dirnames(p, dirnames)
        if p == md_dir:
            continue
        all_dirs.append(p)
    all_dirs.sort(key=lambda d: len(d.parts), reverse=True)
    for d in all_dirs:
        try:
            if d.is_symlink() or d.is_junction():
                continue
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def _pdf_link_target(md_dir: Path, pdf_path: str | Path | None, pdf_name: str) -> str:
    """이 md 파일 위치(md_dir/<field>/<파일>.md, 항상 md_dir 바로 한 단 아래)에서
    --pdf 절대 경로까지의 상대 경로. 다른 드라이브면 file:/// 절대 URI(N6).

    pdf_path 가 없으면(예: selftest_md 의 합성 result) 이전처럼 파일명만 쓴다.
    """
    if not pdf_path:
        return pdf_name
    try:
        pdf_abs = Path(pdf_path).resolve()
        # 산출되는 모든 md 파일은 md_dir 바로 한 단 아래(md_dir/<field>/<파일>.md)에
        # 있으므로, 실제로 존재하지 않는 자리표시 폴더 하나만 붙여도 상대경로 "깊이"는
        # 정확하다(os.path.relpath 는 실제 존재 여부를 보지 않는다).
        file_dir = Path(md_dir).resolve() / "_field_"
        rel = os.path.relpath(pdf_abs, start=file_dir)
        # 코치 핫픽스 09-17(리뷰 확인): 파일·폴더 이름의 % # ? 가 뒤에 붙는 #page=N 과
        # 섞이지 않게 백분율 부호화한다(한글·공백은 <…> 링크 안에서 그대로 둔다).
        return Path(rel).as_posix().replace("%", "%25").replace("#", "%23").replace("?", "%3F")
    except (ValueError, OSError):
        try:
            return Path(pdf_path).resolve().as_uri()
        except Exception:  # noqa: BLE001
            return pdf_name


def _build_md_contents(
    result: dict, pdf_name: str, md_dir: Path, pdf_path: str | Path | None = None
) -> dict[str, str]:
    """레코드를 대분류별 Markdown 텍스트로 구성해 {md 기준 상대경로(posix): 내용} 을 돌려준다."""
    records: list[dict] = result.get("records") or []
    groups: list[dict] = result.get("groups") or []
    subheaders: list[dict] = result.get("subheaders") or []
    half = result.get("half") or ""
    sha = result.get("sha256") or ""
    pages = result.get("pages") or []
    layout = pages[0]["layout"] if pages else ""
    page_nums = [p["pdf_page"] for p in pages]
    page_range = f"{min(page_nums)}-{max(page_nums)}" if page_nums else ""
    pdf_rel = _pdf_link_target(md_dir, pdf_path, pdf_name)

    def _link(pdf_page: int | None) -> str:
        if pdf_page is None:
            return ""
        return f"[p.{pdf_page}](<{pdf_rel}#page={pdf_page}>)"

    gmap = {g["group_id"]: g for g in groups}
    by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in records:
        g = gmap.get(r.get("group_id")) or {}
        field_ = g.get("field") or r.get("field") or "_"
        major = g.get("major") or r.get("major") or r["code"][0]
        major_name = g.get("major_name") or ""
        by_key[(field_, major, major_name)].append(r)

    group_order = [g["group_id"] for g in groups]

    subs_by_group: dict[str, list[dict]] = defaultdict(list)
    for s in subheaders:
        subs_by_group[s.get("group_id") or "_"].append(s)

    contents: dict[str, str] = {}
    for (field_, major, major_name), recs in by_key.items():
        # field/major 는 PDF 파싱 결과에서 온 값이라 신뢰하지 않는다 — 경로 구분자나
        # ".." 가 섞여 있어도 폴더/파일명 한 칸으로만 쓰이도록 살균한다(예약 폴더명
        # _preserved 와 겹치는 경우도 여기서 걸러진다). 여기서 놓치더라도 render_md()
        # 의 _safe_join() 최종 검사가 md 밖 쓰기를 막는다.
        safe_field = _safe_component(field_)
        safe_major = _safe_component(major)
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", major_name or "")
        fname = f"{safe_major}_{safe_name}.md" if safe_name else f"{safe_major}_.md"
        relpath = f"{safe_field}/{fname}"

        buckets: dict[str, list[dict]] = defaultdict(list)
        for r in recs:
            buckets[r.get("group_id") or "_"].append(r)
        ordered_ids = [gid for gid in group_order if gid in buckets]
        for gid in buckets:
            if gid not in ordered_ids:
                ordered_ids.append(gid)

        lines = [
            "---",
            f"half: {half}",
            f"source: {pdf_name}",
            f"sha256: {sha}",
            f"pages: {page_range}",
            f"layout: {layout}",
            "---",
            "",
        ]
        for gid in ordered_ids:
            g = gmap.get(gid) or {}
            header = g.get("header") or ""
            gmajor = g.get("major") or major
            gname = g.get("major_name") or major_name
            printed = g.get("printed_page")
            extra = f" (인쇄 {printed}쪽)" if printed else ""
            lines.append(f"## ■ {_esc_text(header)}   (대분류 {_esc_text(gmajor)} {_esc_text(gname)}){extra}")  # 코치 핫픽스 09-17: 제목 줄도 F6 기준
            lines.append("")
            group_recs = buckets[gid]
            # N4: 이 그룹에 비고가 있는 레코드가 하나라도 있으면 비고 열을 더한다.
            has_remark = any((r.get("remark") or "").strip() for r in group_recs)
            if has_remark:
                lines.append("| 공종코드 | 공종명칭 | 규격 | 단위 | 단가 | 노무비율 | 비고 | 원문 |")
                lines.append("|---|---|---|---|---|---|---|---|")
            else:
                lines.append("| 공종코드 | 공종명칭 | 규격 | 단위 | 단가 | 노무비율 | 원문 |")
                lines.append("|---|---|---|---|---|---|---|")
            # N4: 소제목(subheaders.jsonl) 행을 그 소제목을 이어받는 레코드들 앞에
            # 한 행으로 넣는다(공종코드 칸 = 코드 패턴, 공종명칭 칸 = 굵게 소제목 텍스트).
            sub_map = {s.get("code_pattern"): s for s in subs_by_group.get(gid, [])}
            emitted_patterns: set[str] = set()
            for r in group_recs:
                ng = r.get("name_group")
                if ng and ng in sub_map and ng not in emitted_patterns:
                    s = sub_map[ng]
                    emitted_patterns.add(ng)
                    sub_cells = [
                        _esc(s.get("code_pattern")),
                        f"**{_esc(s.get('text'))}**",
                        "",
                        "",
                        "",
                        "",
                    ]
                    if has_remark:
                        sub_cells.append("")
                    sub_cells.append(_link(s.get("pdf_page")))
                    lines.append("| " + " | ".join(sub_cells) + " |")
                if r.get("status") == "abolished":
                    price_cell = r.get("price_raw") or "폐지"
                    labor_cell = r.get("abolished_at") or r.get("labor_raw") or ""
                else:
                    price_cell = r.get("price_raw") or ""
                    labor_cell = r.get("labor_raw") or ""
                cells = [
                    _esc(r.get("code")),
                    _esc(r.get("name")),
                    _esc(r.get("spec")),
                    _esc(r.get("unit")),
                    _esc(price_cell),
                    _esc(labor_cell),
                ]
                if has_remark:
                    cells.append(_esc(r.get("remark")))
                cells.append(_link(r.get("pdf_page")))
                lines.append("| " + " | ".join(cells) + " |")
            notes = g.get("notes") or []
            figures = g.get("figures") or []
            if notes:
                lines.append("")
                lines.append("**【단가정의】**")
                lines.append("")
                # N5: 주석 한 항목이 뷰어에서도 한 줄로 분리되도록 목록(- )으로 적는다.
                for n in notes:
                    item_txt = _esc_text(n.get("item"))
                    if item_txt:
                        lines.append(f"- {item_txt}")
            if figures:
                lines.append("")
                for fig in figures:
                    cap = _esc_text(fig.get("caption"))
                    if cap:
                        lines.append(f"- 그림: {cap}")
            lines.append("")

        contents[relpath] = "\n".join(lines)
    return contents


def render_md(
    result: dict,
    md_dir: str | Path,
    pdf_name: str,
    pdf_path: str | Path | None = None,
) -> MdSummary:
    md_dir = Path(md_dir)
    summary = MdSummary()

    if md_dir.exists() and not md_dir.is_dir():
        raise MdOutputError(
            f"{md_dir} 가 폴더가 아니라 파일입니다. 아무것도 바꾸지 않고 중단합니다."
        )
    md_dir.mkdir(parents=True, exist_ok=True)

    # md 의 "진짜" 위치를 이번 실행 시작 시 딱 한 번만 확정한다. 이후 모든 안전성
    # 비교는 이 고정값을 기준으로 하고, 절대 다시 계산하지 않는다(경합 방어 설계 메모 참고).
    md_resolved = md_dir.resolve()

    new_contents = _build_md_contents(result, pdf_name, md_dir, pdf_path)

    # 이번 실행의 산출물끼리 대소문자만 다른 경로로 충돌할 수 있다(예: PDF 파싱된
    # field/major 값이 "AB"/"ab" 처럼 대소문자만 다르게 나온 경우). Windows 는 대소문자를
    # 구분하지 않으므로 이 둘은 물리적으로 같은 파일이다. 그대로 두면 규칙 2 쓰기
    # 루프가 먼저 쓴 쪽을 "기존 파일"로 보고 목록(이전 실행분)의 해시와 비교하다가
    # 방금 자신이 쓴 정상 산출물을 사용자 수정으로 오판해 _preserved 로 밀어버리고,
    # 최종 목록에는 디스크에 없는 내용의 해시가 그대로 기록되는(목록≠디스크) 조용한
    # 데이터 유실로 이어진다. 그런 키가 있으면 경고하고, 데이터를 잃지 않도록 두
    # 내용을 하나로 합쳐 단일 파일/단일 목록 항목으로 만든다.
    cf_new_groups: dict[str, list[str]] = defaultdict(list)
    for k in new_contents:
        cf_new_groups[k.casefold()].append(k)
    for _cf, keys in cf_new_groups.items():
        if len(keys) > 1:
            keys_sorted = sorted(keys)
            canonical = keys_sorted[0]
            summary.warnings.append(
                "이번 실행 산출물끼리 대소문자만 다른 경로가 충돌해 하나로 합칩니다: "
                + ", ".join(keys_sorted)
            )
            merged = "\n".join(new_contents[k] for k in keys_sorted)
            for k in keys_sorted:
                del new_contents[k]
            new_contents[canonical] = merged

    manifest, load_warnings, manifest_state = _load_manifest(md_dir, md_resolved)
    summary.warnings.extend(load_warnings)

    # Windows 는 대소문자를 구분하지 않으므로, 목록 항목과 이번 실행의 경로가
    # 대소문자만 다르더라도 "같은 파일"로 판정해야 한다(안 그러면 방금 쓴 파일을
    # 목록에 없는 것으로 오판해 지우게 된다). 아래 두 맵은 casefold 기준 조회용.
    manifest_cf: dict[str, str] = {k.casefold(): v for k, v in manifest.items()}
    new_cf_keys: set[str] = {k.casefold() for k in new_contents}

    preserved_root_cache: list[Path] = []

    def get_preserved_root() -> Path:
        if not preserved_root_cache:
            preserved_root_cache.append(_pick_preserved_root(md_dir))
        return preserved_root_cache[0]

    # --- 규칙 2: 쓰기 전 판정, 실제로 쓴다 ---
    # target 은 _safe_join() 으로 얻는다: field/major 는 이미 _safe_component() 로
    # 살균했지만(위), 이 검사가 최종 방어선이다 — 실제로 계산될 경로가 md_dir 의
    # realpath 안에 있는지(경로 중간에 낀 정션(junction)까지 따라가서) 확인하고,
    # 하나라도 벗어나면 그 파일은 쓰지 않고 경고만 남긴다.
    for relpath in list(new_contents.keys()):
        content = new_contents[relpath]
        target = _safe_join(md_dir, relpath, md_resolved)
        if target is None:
            summary.warnings.append(
                f"산출 경로가 md 밖을 가리키거나 안전하지 않아 건너뜁니다: {relpath}"
            )
            del new_contents[relpath]
            continue
        data = content.encode("utf-8")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # mkdir 이 경합 중 심어진 정션을 따라갔을 수 있으므로 곧바로 다시 확인한다.
            reverified = _safe_join(md_dir, relpath, md_resolved)
            if reverified is None or reverified != target:
                raise _RaceGuardError(f"{relpath}: mkdir 이후 재검증 실패(경합 의심)")
            if target.exists():
                current_bytes = _read_verified(md_dir, md_resolved, relpath, target)
                current_hash = _sha256(current_bytes)
                manifest_hash = manifest_cf.get(relpath.casefold())
                if manifest_hash is not None and current_hash == manifest_hash:
                    reverified = _safe_join(md_dir, relpath, md_resolved)
                    if reverified is None:
                        raise _RaceGuardError(f"{relpath}: 쓰기 직전 재검증 실패(경합 의심)")
                    target.write_bytes(data)
                    summary.overwritten += 1
                else:
                    _preserve_move(md_dir, md_resolved, relpath, get_preserved_root())
                    summary.preserved += 1  # 코치 핫픽스 09-17: 옮기기 성공 즉시 센다(뒤 쓰기가 실패해도 보존 사실이 요약에 남게)
                    reverified = _safe_join(md_dir, relpath, md_resolved)
                    if reverified is None:
                        raise _RaceGuardError(f"{relpath}: 보존 후 쓰기 직전 재검증 실패(경합 의심)")
                    target.write_bytes(data)
            else:
                reverified = _safe_join(md_dir, relpath, md_resolved)
                if reverified is None:
                    raise _RaceGuardError(f"{relpath}: 쓰기 직전 재검증 실패(경합 의심)")
                target.write_bytes(data)
                summary.written += 1
        except (OSError, _RaceGuardError) as e:
            summary.warnings.append(
                f"{relpath} 을(를) 쓰는 중 오류가 발생해 건너뜁니다"
                f"(파일이 잠겨 있거나 경합이 의심됨): {e}"
            )
            del new_contents[relpath]
            continue

    # --- 규칙 3: 목록에는 있지만 이번에 안 쓰는 경로 정리 ---
    for relpath, old_hash in manifest.items():
        if relpath.casefold() in new_cf_keys:
            continue
        target = _safe_join(md_dir, relpath, md_resolved)
        if target is None:
            continue  # 이미 _load_manifest 에서 경고하고 걸러짐
        try:
            if target.is_symlink() or target.is_junction():
                _preserve_move(md_dir, md_resolved, relpath, get_preserved_root())
                summary.preserved += 1
                continue
            try:
                current_bytes = _read_verified(md_dir, md_resolved, relpath, target)
            except FileNotFoundError:
                continue  # "없다 -> 넘어간다"
            current_hash = _sha256(current_bytes)
            if current_hash == old_hash:
                reverified = _safe_join(md_dir, relpath, md_resolved)
                if reverified is None or reverified.is_symlink() or reverified.is_junction():
                    raise _RaceGuardError(f"{relpath}: 삭제 직전 재검증 실패(경합 의심)")
                os.remove(target)
                summary.deleted += 1
            else:
                _preserve_move(md_dir, md_resolved, relpath, get_preserved_root())
                summary.preserved += 1
        except (OSError, _RaceGuardError) as e:
            summary.warnings.append(
                f"{relpath} 정리 중 오류가 발생해 건너뜁니다"
                f"(파일이 잠겨 있거나 경합이 의심됨): {e}"
            )
            continue

    # --- 규칙 6: 이번 정리로 빈 하위 폴더만 정리 ---
    _remove_empty_dirs(md_dir)

    # --- 규칙 5: 목록이 없었으면(없음/깨짐) 정리 후보만 경고 ---
    if manifest_state != "valid":
        for relpath, _fp in _walk_files(md_dir):
            if relpath == MANIFEST_NAME:
                continue
            if not relpath.endswith(".md"):
                continue
            if relpath in new_contents:
                continue
            summary.stale_candidates += 1
            summary.warnings.append(f"목록이 없어 정리하지 않은 기존 산출 후보: {relpath}")

    # --- 건드리지 않은 파일 집계(규칙 4) ---
    for relpath, _fp in _walk_files(md_dir):
        if relpath == MANIFEST_NAME:
            continue
        if relpath in new_contents:
            continue
        summary.untouched += 1

    # --- 규칙 1: 목록 파일을 이번 산출 기준으로 다시 쓴다 ---
    manifest_out = {
        "tool": TOOL_NAME,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": {
            relpath: _sha256(content.encode("utf-8"))
            for relpath, content in sorted(new_contents.items())
        },
    }
    (md_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest_out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return summary
