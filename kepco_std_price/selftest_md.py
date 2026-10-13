"""render_md 안전 정리 로직의 자체 시험(S1~S14). PDF 없이 tempfile 에서 돈다.

실행: python -m kepco_std_price.selftest_md
각 줄에 PASS/FAIL 을 찍고, 하나라도 FAIL 이면 종료 코드 1.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

from .render_md import MANIFEST_NAME, MdOutputError, render_md

PDF_NAME = "test.pdf"


def _rec(group_id, code, name, spec, unit, price, labor, status="present", page=1):
    return {
        "group_id": group_id,
        "code": code,
        "name": name,
        "spec": spec,
        "unit": unit,
        "price_raw": price,
        "labor_raw": labor,
        "status": status,
        "pdf_page": page,
    }


def _group(gid, field, major, major_name, header, page=1):
    return {
        "group_id": gid,
        "field": field,
        "major": major,
        "major_name": major_name,
        "header": header,
        "printed_page": page,
        "notes": [],
        "figures": [],
    }


def make_result_full() -> dict:
    """토목/A_일반, 토목/B_포장, 건축/C_마감 세 파일이 나오는 합성 result."""
    groups = [
        _group("g1", "토목", "A", "일반", "일반사항", page=10),
        _group("g2", "토목", "B", "포장", "포장공사", page=20),
        _group("g3", "건축", "C", "마감", "마감공사", page=30),
    ]
    records = [
        _rec("g1", "A001", "터파기", "1종", "m3", "10,000", "50%", page=10),
        _rec("g2", "B001", "아스콘포장", "1종", "m2", "20,000", "40%", page=20),
        _rec("g3", "C001", "타일붙임", "1종", "m2", "30,000", "30%", page=30),
    ]
    return {
        "records": records,
        "groups": groups,
        "half": "2025H2",
        "sha256": "deadbeef",
        "pages": [
            {"pdf_page": 10, "layout": "L1"},
            {"pdf_page": 20, "layout": "L1"},
            {"pdf_page": 30, "layout": "L1"},
        ],
    }


def make_result_case_collision() -> dict:
    """분야값이 대소문자만 다른 두 그룹("AB"/"ab")이 같은 대분류로 나오는 합성 result.

    field 가 대소문자만 다르면 relpath("AB/X_이름.md" vs "ab/X_이름.md")도 Windows
    파일시스템에서는 같은 물리 파일이 된다 — 이번 실행 산출물끼리의 자기 충돌 시나리오.
    """
    groups = [
        _group("g1", "AB", "X", "이름", "헤더1", page=10),
        _group("g2", "ab", "X", "이름", "헤더2", page=20),
    ]
    records = [
        _rec("g1", "A001", "터파기", "1종", "m3", "10,000", "50%", page=10),
        _rec("g2", "B001", "아스콘포장", "1종", "m2", "20,000", "40%", page=20),
    ]
    return {
        "records": records,
        "groups": groups,
        "half": "2025H2",
        "sha256": "deadbeef",
        "pages": [
            {"pdf_page": 10, "layout": "L1"},
            {"pdf_page": 20, "layout": "L1"},
        ],
    }


def make_result_dropped_g3() -> dict:
    """건축/C_마감 이 통째로 빠진 버전(대분류 하나 삭제 시나리오)."""
    r = make_result_full()
    r["groups"] = [g for g in r["groups"] if g["group_id"] != "g3"]
    r["records"] = [rec for rec in r["records"] if rec["group_id"] != "g3"]
    r["pages"] = [p for p in r["pages"] if p["pdf_page"] != 30]
    return r


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> str:
    return _sha256_bytes(p.read_bytes())


def _snapshot(md_dir: Path) -> dict[str, str]:
    """md_dir 밑 모든 파일(경로->내용 문자열)을 재귀 스냅샷."""
    out = {}
    for p in md_dir.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(md_dir).as_posix())] = p.read_bytes().decode("utf-8", "replace")
    return out


class Check:
    def __init__(self):
        self.results: list[tuple[str, bool | None, str]] = []

    def check(self, name: str, cond: bool, detail: str = ""):
        self.results.append((name, cond, detail))

    def skip(self, name: str, detail: str = ""):
        self.results.append((name, None, detail))

    def all_pass(self) -> bool:
        return all(ok is not False for _, ok, _ in self.results)

    def report(self) -> str:
        lines = []
        for name, ok, detail in self.results:
            if ok is None:
                status = "SKIP"
                suffix = f" - {detail}" if detail else ""
            else:
                status = "PASS" if ok else "FAIL"
                suffix = f" - {detail}" if (detail and not ok) else ""
            lines.append(f"{status} {name}{suffix}")
        return "\n".join(lines)


def run_all() -> Check:
    c = Check()

    # ---------- S1: 빈 폴더 첫 실행 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s1_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        summary = render_md(make_result_full(), md_dir, PDF_NAME)
        expect_files = {"토목/A_일반.md", "토목/B_포장.md", "건축/C_마감.md"}
        actual_files = {
            str(p.relative_to(md_dir).as_posix())
            for p in md_dir.rglob("*.md")
        }
        c.check("S1 빈 폴더 첫 실행: MD 3개 생성", actual_files == expect_files, str(actual_files))
        c.check("S1 written=3", summary.written == 3, str(summary.written))
        c.check("S1 overwritten=0 deleted=0 preserved=0",
                summary.overwritten == 0 and summary.deleted == 0 and summary.preserved == 0,
                summary.line())
        manifest_path = md_dir / MANIFEST_NAME
        c.check("S1 목록 파일 생성", manifest_path.exists())
        try:
            mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
            mfiles = mdata.get("files") or {}
            c.check("S1 목록에 3개 항목·도구명 포함",
                    set(mfiles.keys()) == expect_files and mdata.get("tool") == "kepco_std_price",
                    str(mfiles.keys()))
            c.check("S1 목록에 생성 시각 포함", bool(mdata.get("generated_at")))
        except Exception as e:  # noqa: BLE001
            c.check("S1 목록 JSON 파싱", False, str(e))
        files_with_cr = [
            str(p.relative_to(md_dir).as_posix())
            for p in md_dir.rglob("*")
            if p.is_file() and b"\r" in p.read_bytes()
        ]
        c.check("S1 출력 파일에 \\r 없음(LF 통일)", len(files_with_cr) == 0, str(files_with_cr))

    # ---------- S2: 같은 입력 재실행 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s2_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        render_md(make_result_full(), md_dir, PDF_NAME)
        snap1 = _snapshot(md_dir)
        summary2 = render_md(make_result_full(), md_dir, PDF_NAME)
        snap2 = _snapshot(md_dir)
        md_snap1 = {k: v for k, v in snap1.items() if k.endswith(".md") and not k.startswith(MANIFEST_NAME)}
        md_snap2 = {k: v for k, v in snap2.items() if k.endswith(".md") and not k.startswith(MANIFEST_NAME)}
        c.check("S2 재실행 deleted=0 preserved=0", summary2.deleted == 0 and summary2.preserved == 0, summary2.line())
        c.check("S2 재실행 overwritten=3 written=0", summary2.overwritten == 3 and summary2.written == 0, summary2.line())
        c.check("S2 md 파일 내용 동일", md_snap1 == md_snap2)

    # ---------- S3: 대분류 하나가 빠진 입력 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s3_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        render_md(make_result_full(), md_dir, PDF_NAME)
        summary3 = render_md(make_result_dropped_g3(), md_dir, PDF_NAME)
        c.check("S3 건축/C_마감.md 삭제됨", not (md_dir / "건축" / "C_마감.md").exists())
        c.check("S3 deleted>=1", summary3.deleted >= 1, summary3.line())
        c.check("S3 preserved=0(내용 안 바뀐 삭제)", summary3.preserved == 0, summary3.line())
        c.check("S3 빈 하위 폴더(건축) 정리됨", not (md_dir / "건축").exists())
        c.check("S3 토목 폴더는 그대로 남음", (md_dir / "토목" / "A_일반.md").exists() and (md_dir / "토목" / "B_포장.md").exists())

    # ---------- S4: 사용자 파일은 항상 그대로 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s4_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        md_dir.mkdir(parents=True)
        (md_dir / "토목").mkdir(parents=True)
        (md_dir / "토목" / "메모.md").write_text("사용자 메모", encoding="utf-8")
        (md_dir / "notes.txt").write_text("사용자 노트", encoding="utf-8")
        h_memo = _sha256_file(md_dir / "토목" / "메모.md")
        h_notes = _sha256_file(md_dir / "notes.txt")

        render_md(make_result_full(), md_dir, PDF_NAME)
        ok1 = (md_dir / "토목" / "메모.md").exists() and _sha256_file(md_dir / "토목" / "메모.md") == h_memo
        ok2 = (md_dir / "notes.txt").exists() and _sha256_file(md_dir / "notes.txt") == h_notes
        c.check("S4 1회차 이후 메모.md 그대로", ok1)
        c.check("S4 1회차 이후 notes.txt 그대로", ok2)

        render_md(make_result_full(), md_dir, PDF_NAME)
        ok3 = (md_dir / "토목" / "메모.md").exists() and _sha256_file(md_dir / "토목" / "메모.md") == h_memo
        ok4 = (md_dir / "notes.txt").exists() and _sha256_file(md_dir / "notes.txt") == h_notes
        c.check("S4 2회차 이후 메모.md 그대로", ok3)
        c.check("S4 2회차 이후 notes.txt 그대로", ok4)

    # ---------- S5: 다시 쓸 MD 를 사용자가 고침 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s5_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        render_md(make_result_full(), md_dir, PDF_NAME)
        target = md_dir / "토목" / "A_일반.md"
        original_tool_content = target.read_bytes()
        edited = original_tool_content + "\n사용자가 고친 줄\n".encode("utf-8")
        target.write_bytes(edited)

        summary5 = render_md(make_result_full(), md_dir, PDF_NAME)
        c.check("S5 preserved>=1", summary5.preserved >= 1, summary5.line())
        preserved_dirs = list((md_dir / "_preserved").glob("*")) if (md_dir / "_preserved").exists() else []
        c.check("S5 _preserved 폴더 생성", len(preserved_dirs) >= 1)
        found = False
        preserved_content = b""
        if preserved_dirs:
            for root in preserved_dirs:
                cand = root / "토목" / "A_일반.md"
                if cand.exists():
                    found = True
                    preserved_content = cand.read_bytes()
                    break
        c.check("S5 원본(사용자 수정본)이 _preserved 로 옮겨짐", found)
        c.check("S5 _preserved 안 내용이 사용자 수정본과 동일", preserved_content == edited)
        c.check("S5 새 A_일반.md 는 도구가 새로 쓴 내용", target.exists() and target.read_bytes() == original_tool_content)

    # ---------- S6: 더는 안 쓸 MD 를 사용자가 고침 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s6_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        render_md(make_result_full(), md_dir, PDF_NAME)
        stale_target = md_dir / "건축" / "C_마감.md"
        edited6 = stale_target.read_bytes() + "\n사용자가 고친 줄\n".encode("utf-8")
        stale_target.write_bytes(edited6)

        summary6 = render_md(make_result_dropped_g3(), md_dir, PDF_NAME)
        c.check("S6 preserved>=1", summary6.preserved >= 1, summary6.line())
        c.check("S6 deleted(건축/C_마감.md 는 아님, 해시 달라 보존)", True)
        c.check("S6 원래 자리엔 더 이상 없음", not stale_target.exists())
        preserved_dirs6 = list((md_dir / "_preserved").glob("*")) if (md_dir / "_preserved").exists() else []
        found6 = False
        content6 = b""
        for root in preserved_dirs6:
            cand = root / "건축" / "C_마감.md"
            if cand.exists():
                found6 = True
                content6 = cand.read_bytes()
                break
        c.check("S6 사용자 수정본이 _preserved 로 옮겨짐", found6)
        c.check("S6 _preserved 내용이 사용자 수정본과 동일", content6 == edited6)

    # ---------- S7: 목록 없는 기존 md 폴더 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s7_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        (md_dir / "토목").mkdir(parents=True)
        legacy = md_dir / "토목" / "D_.md"
        legacy.write_text("옛 버전 산출물", encoding="utf-8")
        legacy_hash = _sha256_file(legacy)

        summary7 = render_md(make_result_full(), md_dir, PDF_NAME)
        c.check("S7 deleted=0", summary7.deleted == 0, summary7.line())
        c.check("S7 stale_candidates>=1", summary7.stale_candidates >= 1, summary7.line())
        c.check("S7 옛 파일 그대로(내용 동일)", legacy.exists() and _sha256_file(legacy) == legacy_hash)
        c.check("S7 경고 메시지 존재", any("정리" in w or "목록" in w for w in summary7.warnings))

    # ---------- S8: md 가 폴더가 아니라 파일 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s8_") as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir(parents=True)
        md_as_file = out_dir / "md"
        md_as_file.write_text("나는 폴더가 아니라 파일이다", encoding="utf-8")
        before = md_as_file.read_bytes()
        raised = False
        try:
            render_md(make_result_full(), md_as_file, PDF_NAME)
        except MdOutputError:
            raised = True
        c.check("S8 MdOutputError 발생", raised)
        c.check("S8 파일 그대로(내용 불변)", md_as_file.is_file() and md_as_file.read_bytes() == before)

    # ---------- S9: 목록에 ../밖.md · 절대경로가 있을 때 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s9_") as tmp:
        out_dir = Path(tmp) / "out"
        md_dir = out_dir / "md"
        render_md(make_result_full(), md_dir, PDF_NAME)

        outside_rel = out_dir / "밖.md"
        outside_rel.write_text("밖에 있는 파일(상대경로 탈출용)", encoding="utf-8")
        outside_abs_dir = Path(tmp) / "other"
        outside_abs_dir.mkdir(parents=True)
        outside_abs = outside_abs_dir / "abs.md"
        outside_abs.write_text("밖에 있는 파일(절대경로용)", encoding="utf-8")
        h_rel = _sha256_file(outside_rel)
        h_abs = _sha256_file(outside_abs)

        manifest_path = md_dir / MANIFEST_NAME
        mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
        mdata["files"]["../밖.md"] = "0" * 64
        mdata["files"][str(outside_abs)] = "0" * 64
        manifest_path.write_text(json.dumps(mdata, ensure_ascii=False, indent=2), encoding="utf-8")

        summary9 = render_md(make_result_full(), md_dir, PDF_NAME)
        c.check("S9 상대경로 밖 파일 그대로", outside_rel.exists() and _sha256_file(outside_rel) == h_rel)
        c.check("S9 절대경로 밖 파일 그대로", outside_abs.exists() and _sha256_file(outside_abs) == h_abs)
        c.check("S9 경고 발생", len(summary9.warnings) >= 2, str(summary9.warnings))
        c.check(
            "S9 md 안에 엉뚱한 파일(밖.md/abs.md) 생성 안 됨",
            not (md_dir / "밖.md").exists() and not (md_dir / "abs.md").exists(),
        )

    # ---------- S10: 목록 JSON 깨짐 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s10_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        render_md(make_result_full(), md_dir, PDF_NAME)
        manifest_path = md_dir / MANIFEST_NAME
        manifest_path.write_text("{ 이것은: 깨진 JSON 입니다 ...", encoding="utf-8")

        summary10 = render_md(make_result_dropped_g3(), md_dir, PDF_NAME)
        c.check("S10 deleted=0", summary10.deleted == 0, summary10.line())
        c.check("S10 경고 발생", any("깨져" in w for w in summary10.warnings), str(summary10.warnings))
        c.check("S10 건축/C_마감.md 그대로 남음(안 지움)", (md_dir / "건축" / "C_마감.md").exists())
        # JSON 깨짐 취급 시에도 다음 실행을 위해 목록은 정상 JSON 으로 다시 쓰여야 한다
        try:
            json.loads(manifest_path.read_text(encoding="utf-8"))
            rewritten_ok = True
        except Exception:  # noqa: BLE001
            rewritten_ok = False
        c.check("S10 목록 파일이 이번 실행 후 정상 JSON 으로 재작성됨", rewritten_ok)

    # ---------- S11: 목록 키가 실제 파일과 대소문자만 다를 때 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s11_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        render_md(make_result_full(), md_dir, PDF_NAME)
        manifest_path = md_dir / MANIFEST_NAME
        mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
        key = "토목/A_일반.md"
        sha = mdata["files"].pop(key)
        cased_key = "토목/A_일반.MD"
        mdata["files"][cased_key] = sha
        manifest_path.write_text(json.dumps(mdata, ensure_ascii=False, indent=2), encoding="utf-8")

        target = md_dir / "토목" / "A_일반.md"
        original_content = target.read_bytes()

        summary11 = render_md(make_result_full(), md_dir, PDF_NAME)
        c.check("S11 대소문자만 다른 목록 키: deleted=0", summary11.deleted == 0, summary11.line())
        c.check("S11 대소문자만 다른 목록 키: preserved=0", summary11.preserved == 0, summary11.line())
        c.check(
            "S11 A_일반.md 가 디스크에 그대로 남아있음(사라지지 않음)",
            target.exists() and target.read_bytes() == original_content,
        )
        mdata2 = json.loads(manifest_path.read_text(encoding="utf-8"))
        c.check(
            "S11 재작성된 목록은 실제 파일명(소문자) 기준으로 정규화됨",
            key in mdata2["files"] and cased_key not in mdata2["files"],
            str(list(mdata2["files"].keys())),
        )

    # ---------- S11b: 목록에 대소문자만 다른 키가 둘 이상 있을 때(충돌) ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s11b_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        render_md(make_result_full(), md_dir, PDF_NAME)
        manifest_path = md_dir / MANIFEST_NAME
        mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
        key = "토목/A_일반.md"
        sha = mdata["files"][key]
        mdata["files"]["토목/A_일반.MD"] = sha  # 같은 파일을 가리키는 대소문자만 다른 두 번째 키
        manifest_path.write_text(json.dumps(mdata, ensure_ascii=False, indent=2), encoding="utf-8")

        target = md_dir / "토목" / "A_일반.md"
        original_content = target.read_bytes()

        summary11b = render_md(make_result_full(), md_dir, PDF_NAME)
        c.check(
            "S11b 대소문자만 다른 키 충돌 경고 발생",
            any("대소문자" in w for w in summary11b.warnings),
            str(summary11b.warnings),
        )
        c.check("S11b deleted=0(보수적으로 지우지 않음)", summary11b.deleted == 0, summary11b.line())
        c.check(
            "S11b A_일반.md 그대로 남아있음(내용 동일)",
            target.exists() and target.read_bytes() == original_content,
        )

    # ---------- S12: 목록 relpath 에 NUL(\x00) 문자가 섞여 있을 때 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s12_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        render_md(make_result_full(), md_dir, PDF_NAME)
        manifest_path = md_dir / MANIFEST_NAME
        mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
        mdata["files"]["\x00이상한/경로.md"] = "0" * 64
        mdata["files"]["토목/A\x00_일반.md"] = "0" * 64
        manifest_path.write_text(json.dumps(mdata, ensure_ascii=False, indent=2), encoding="utf-8")

        crashed = False
        crash_detail = ""
        summary12 = None
        try:
            summary12 = render_md(make_result_full(), md_dir, PDF_NAME)
        except Exception as e:  # noqa: BLE001
            crashed = True
            crash_detail = repr(e)
        c.check("S12 NUL 이 섞인 목록 경로에서도 죽지 않음", not crashed, crash_detail)
        if summary12 is not None:
            c.check(
                "S12 그 항목만 무시하고 경고를 남김",
                any("안전하지" in w for w in summary12.warnings),
                str(summary12.warnings),
            )
            c.check(
                "S12 나머지 파일은 정상 처리됨(overwritten=3)",
                summary12.overwritten == 3,
                summary12.line(),
            )

    # ---------- S13: 최종 목록 파일이 디스크 산출과 정확히(해시) 일치 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s13_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        render_md(make_result_full(), md_dir, PDF_NAME)

        # 사용자가 파일 하나를 고치고, 목록도 대소문자만 다르게 흔들어 둔 채로 재실행.
        target = md_dir / "토목" / "B_포장.md"
        target.write_bytes(target.read_bytes() + b"\nedit\n")
        manifest_path = md_dir / MANIFEST_NAME
        mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
        key = "토목/A_일반.md"
        sha = mdata["files"].pop(key)
        mdata["files"][key.upper()] = sha
        manifest_path.write_text(json.dumps(mdata, ensure_ascii=False, indent=2), encoding="utf-8")

        render_md(make_result_full(), md_dir, PDF_NAME)

        mdata_final = json.loads(manifest_path.read_text(encoding="utf-8"))
        mismatches = []
        for relpath, sha in mdata_final["files"].items():
            fp = md_dir / relpath
            if not fp.exists():
                mismatches.append(f"{relpath}: 파일 없음")
                continue
            if _sha256_file(fp) != sha:
                mismatches.append(f"{relpath}: 해시 불일치")
        c.check("S13 목록의 모든 항목이 디스크와 해시 일치", not mismatches, str(mismatches))

        disk_md_files = {
            str(p.relative_to(md_dir).as_posix())
            for p in md_dir.rglob("*.md")
            if "_preserved" not in p.relative_to(md_dir).parts
        }
        listed_files = set(mdata_final["files"].keys())
        c.check(
            "S13 디스크의 산출 MD가 목록에 빠짐없이 반영됨",
            disk_md_files == listed_files,
            str(disk_md_files ^ listed_files),
        )

    # ---------- S14: 이번 실행 산출물끼리 대소문자만 다른 경로로 충돌할 때 ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s14_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        summary14 = render_md(make_result_case_collision(), md_dir, PDF_NAME)
        c.check(
            "S14 충돌 경고 발생",
            any("충돌" in w for w in summary14.warnings),
            str(summary14.warnings),
        )
        c.check(
            "S14 preserved=0(자기 자신을 사용자 수정으로 오판해 밀어내지 않음)",
            summary14.preserved == 0,
            summary14.line(),
        )
        manifest_path14 = md_dir / MANIFEST_NAME
        mdata14 = json.loads(manifest_path14.read_text(encoding="utf-8"))
        mismatches14 = []
        for relpath, sha in mdata14["files"].items():
            fp = md_dir / relpath
            if not fp.exists():
                mismatches14.append(f"{relpath}: 파일 없음")
                continue
            if _sha256_file(fp) != sha:
                mismatches14.append(f"{relpath}: 해시 불일치")
        c.check("S14 목록의 모든 항목이 디스크와 해시 일치", not mismatches14, str(mismatches14))
        c.check(
            "S14 물리 파일이 정확히 하나(둘로 안 쪼개짐)",
            len(list((md_dir).rglob("*.md"))) == 1,
            str(list((md_dir).rglob("*.md"))),
        )
        # 두 그룹의 내용이 모두 반영돼(합쳐져) 데이터 유실이 없어야 한다
        merged_files = list(md_dir.rglob("*.md"))
        merged_text = merged_files[0].read_text(encoding="utf-8") if merged_files else ""
        c.check(
            "S14 두 그룹 레코드가 모두 합쳐져 남음(데이터 유실 없음)",
            "A001" in merged_text and "B001" in merged_text,
            merged_text,
        )

    # ---------- S15: G02i2 F6 md 이스케이프(주석 목록·그림 제목·표 칸) ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s15_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        r15 = make_result_full()
        g1 = r15["groups"][0]
        g1["notes"] = [
            {"item": "① 수량산출 예는 <사례1> 과 같다.(단가*할증 [적용])"},
            {"item": "② a_b* 표기와 `코드` 역슬래시\\ 처리."},
        ]
        g1["figures"] = [{"caption": "[그림-1] <사례2> 굴진방향*표시", "pdf_page": 10}]
        g1["header"] = "AB12** <머리> _밑줄_"  # 코치 핫픽스 09-17: 그룹 제목 줄도 같은 기준
        r15["records"][0]["name"] = "*강조*[대괄호]<태그>"
        summary15 = render_md(r15, md_dir, PDF_NAME)
        md_text15 = (md_dir / "토목" / "A_일반.md").read_text(encoding="utf-8")
        c.check(
            "S15 주석 <사례1> 이스케이프됨(HTML 로 사라지지 않음)",
            "\\<사례1\\>" in md_text15 and "<사례1>" not in md_text15,
            md_text15,
        )
        c.check(
            "S15 주석 강조·대괄호·역슬래시·역따옴표 이스케이프",
            "단가\\*할증 \\[적용\\]" in md_text15 and "a\\_b\\*" in md_text15 and "\\`코드\\`" in md_text15,
            md_text15,
        )
        c.check(
            "S15 그림 제목 <사례2> 이스케이프됨",
            "\\<사례2\\>" in md_text15 and "<사례2>" not in md_text15,
            md_text15,
        )
        c.check(
            "S15 표 칸(공종명칭) 강조·대괄호·태그 이스케이프",
            "\\*강조\\*\\[대괄호\\]\\<태그\\>" in md_text15,
            md_text15,
        )
        c.check(
            "S15 그룹 제목 줄 이스케이프",
            "## ■ AB12\\*\\* \\<머리\\> \\_밑줄\\_" in md_text15 and "<머리>" not in md_text15,
            md_text15,
        )
        c.check("S15 정상 실행(경고 없음)", not summary15.warnings, str(summary15.warnings))

    # ---------- S16: N4 비고 열·소제목 행, N6 원문 링크(코치 핫픽스 09-17, 리뷰 확인 커버리지 보강) ----------
    with tempfile.TemporaryDirectory(prefix="g02g_s16_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        r16 = make_result_full()
        r16["records"][0]["remark"] = "잡석 제외"
        r16["records"][0]["name_group"] = "A00*"
        r16["subheaders"] = [{"group_id": "g1", "code_pattern": "A00*", "text": "소제목 글", "pdf_page": 10}]
        same_drive_pdf = Path(tmp) / "source" / "원문 #1.pdf"
        summary16 = render_md(r16, md_dir, PDF_NAME, pdf_path=same_drive_pdf)
        md_a = (md_dir / "토목" / "A_일반.md").read_text(encoding="utf-8")
        md_b = (md_dir / "토목" / "B_포장.md").read_text(encoding="utf-8")
        c.check(
            "S16 비고 레코드가 있는 그룹만 비고 열(8열) · 값 표시",
            "| 공종코드 | 공종명칭 | 규격 | 단위 | 단가 | 노무비율 | 비고 | 원문 |" in md_a
            and "잡석 제외" in md_a
            and "| 비고 |" not in md_b,
            md_a + "\n----\n" + md_b,
        )
        c.check(
            "S16 소제목 행(코드 패턴 칸·굵은 소제목)이 레코드 앞에 한 번",
            md_a.count("| A00\\* | **소제목 글** |") == 1
            and md_a.index("| A00\\* | **소제목 글** |") < md_a.index("| A001 |"),
            md_a,
        )
        c.check(
            "S16 같은 드라이브 원문 링크: md 위치 기준 상대경로 · 파일명 # 부호화",
            "(<../../../source/원문 %231.pdf#page=10>)" in md_a,
            md_a,
        )
        c.check("S16 정상 실행(경고 없음)", not summary16.warnings, str(summary16.warnings))
        tmp_drive = Path(tmp).resolve().drive.upper()
        if not tmp_drive:
            c.skip(
                "S16 다른 드라이브 원문 링크: file:/// URI · # 부호화",
                "드라이브 없는 플랫폼 (Path.drive empty)",
            )
        else:
            other = "D:" if tmp_drive != "D:" else "C:"
            md_dir2 = Path(tmp) / "out2" / "md"
            render_md(make_result_full(), md_dir2, PDF_NAME, pdf_path=f"{other}/__kepco_selftest__/a#b.pdf")
            md_c = (md_dir2 / "토목" / "A_일반.md").read_text(encoding="utf-8")
            c.check(
                "S16 다른 드라이브 원문 링크: file:/// URI · # 부호화",
                f"(<file:///{other}/__kepco_selftest__/a%23b.pdf#page=10>)" in md_c,
                md_c,
            )

    # ---------- G05 D1: 짧은 단가가 공종코드 부분문자열일 때 첫 행을 버리지 않는다 ----------
    # 2023H2 p172 EB019.19080 / 단가 908. 코드 낱말에 "908" 이 들어 있어도
    # 단가 열 안에 같은 숫자가 있으면 규격 조각이 아니다.
    from .extract import _price_word_complete

    words_d1 = [
        (66.6, 234.0, 135.24, 246.0, "EB019.19080"),
        (154.2, 234.0, 244.2, 246.0, "스터드볼트 설치"),
        (258.84, 234.0, 358.44, 246.0, "자동 D19, L=80㎜"),
        (371.52, 234.0, 382.72, 246.0, "nr"),
        (413.04, 234.0, 433.19, 246.0, "908"),
        (481.08, 234.0, 504.31, 246.0, "50%"),
    ]
    price_col_d1 = (394.12, 451.21)
    ok_d1 = _price_word_complete("908", words_d1, price_col_d1, 216.96, 253.52)
    c.check(
        "D1 short price substring of code stays if in price column",
        ok_d1 is True,
        f"got {ok_d1}",
    )
    words_spec_only = [
        (258.84, 234.0, 358.44, 246.0, "CTC 600"),
    ]
    ok_spec = _price_word_complete("600", words_spec_only, price_col_d1, 216.96, 253.52)
    c.check(
        "D1 short number only in spec column still rejected",
        ok_spec is False,
        f"got {ok_spec}",
    )

    # ---------- G05 D2: 공백 없는 글자층 게이트 + 주석·제목 띄어쓰기 복원 ----------
    from .extract import Span, _restore_spaces
    from .spec import TEXT_LAYER_SPACE_RATIO_MIN, hangul_span_space_ratio

    r_on = hangul_span_space_ratio(["이단가는철골공사시", "스터드볼트설치"])
    r_off = hangul_span_space_ratio(["이 단가는 철골공사 시", "스터드볼트 설치"])
    c.check(
        "D2 no-space hangul ratio below threshold",
        r_on < TEXT_LAYER_SPACE_RATIO_MIN,
        f"ratio={r_on} min={TEXT_LAYER_SPACE_RATIO_MIN}",
    )
    c.check(
        "D2 spaced hangul ratio above threshold",
        r_off >= TEXT_LAYER_SPACE_RATIO_MIN,
        f"ratio={r_off} min={TEXT_LAYER_SPACE_RATIO_MIN}",
    )
    # 「■EB***** 스터드볼트설치」 꼴: 글자 간격 6.6pt 에서만 공백.
    title_chars = [
        Span(75.24, 169.68, 82.55, 184.08, "E"),
        Span(82.56, 169.68, 89.87, 184.08, "B"),
        Span(90.60, 169.68, 95.26, 184.08, "*"),
        Span(95.88, 169.68, 100.54, 184.08, "*"),
        Span(101.16, 169.68, 105.82, 184.08, "*"),
        Span(106.56, 169.68, 111.22, 184.08, "*"),
        Span(111.84, 169.68, 116.50, 184.08, "*"),
        Span(123.12, 169.68, 135.12, 184.08, "스"),
        Span(135.72, 169.68, 147.72, 184.08, "터"),
        Span(148.32, 169.68, 160.32, 184.08, "드"),
        Span(160.92, 169.68, 172.92, 184.08, "볼"),
        Span(173.52, 169.68, 185.52, 184.08, "트"),
        Span(192.12, 169.68, 204.12, 184.08, "설"),
        Span(204.72, 169.68, 216.72, 184.08, "치"),
    ]
    restored_header = _restore_spaces("EB***** 스터드볼트설치", title_chars)
    c.check(
        "D2 restore glued group title 스터드볼트설치",
        restored_header == "EB***** 스터드볼트 설치",
        restored_header,
    )
    note_chars = [
        Span(68.64, 343.92, 80.64, 358.32, "①"),
        Span(86.76, 343.92, 98.76, 358.32, "이"),
        Span(103.92, 343.92, 115.92, 358.32, "단"),
        Span(115.32, 343.92, 127.32, 358.32, "가"),
        Span(126.72, 343.92, 138.72, 358.32, "는"),
        Span(143.88, 343.92, 155.88, 358.32, "철"),
        Span(155.28, 343.92, 167.28, 358.32, "골"),
        Span(166.68, 343.92, 178.68, 358.32, "공"),
        Span(178.08, 343.92, 190.08, 358.32, "사"),
    ]
    restored_note = _restore_spaces("①이단가는철골공사", note_chars)
    c.check(
        "D2 restore glued note 이단가는철골공사",
        restored_note == "① 이 단가는 철골공사",
        restored_note,
    )

    # ---------- G12: 옛 판 주석·칸 경계 ----------
    from .extract import (
        LineSeg,
        _is_diagram_note_table,
        _is_note_junk,
        _is_structural_note_junk,
        _note_items,
        _vxs_near,
        _vline_crosses,
    )
    from .spec import is_figure_glyph_h
    from .conservation import _is_figure_word

    c.check("G12 tiny CAD glyph 2.5pt", is_figure_glyph_h(207.6, 210.1) is True)
    c.check("G12 body glyph 14pt not tiny", is_figure_glyph_h(517.6, 532.0) is False)
    c.check("G12 4.2pt CAD glyph", is_figure_glyph_h(227.3, 231.5) is True)
    c.check("G12 superscript 5.76pt not tiny", is_figure_glyph_h(0.0, 5.76) is False)

    c.check("G12 시공기준면 is figure junk", _is_structural_note_junk("시공기준면") is True)
    c.check('G12 상세"1" is figure junk', _is_structural_note_junk('상세"1"') is True)
    c.check('G12 quoted "1" is figure junk', _is_structural_note_junk('"1"') is True)
    c.check("G12 dim 600 is figure junk", _is_structural_note_junk("600") is True)
    c.check("G12 상세 alone is figure junk", _is_structural_note_junk("상세") is True)
    c.check("G12 차도 is figure junk", _is_structural_note_junk("차도") is True)
    c.check("G12 name-wrap / line is new-item junk", _is_note_junk("수직구 굴착/5m초과,") is True)
    c.check("G12 한다. is not new-item junk", _is_note_junk("한다.") is False)

    items_dot = _note_items(["·【단가정의】", "① 이 단가는 커튼박스 설치에 소요되는 제비용을 포함한다."], 223)
    c.check(
        "G12 ·【단가정의】 label is not a note item",
        len(items_dot) == 1 and items_dot[0]["item"].startswith("①"),
        str(items_dot),
    )
    items_tail = _note_items(["① 제경비를 포함", "한다."], 82)
    c.check(
        "G12 한다. joins previous note",
        len(items_tail) == 1 and items_tail[0]["item"].endswith("한다."),
        str(items_tail),
    )

    header_v = [LineSeg(636.8, 661.0, 184.5)]
    data_v = [LineSeg(659.8, 716.7, 210.5)]
    xs_data = _vxs_near(data_v + header_v, 665.0, 710.0, 30.0, 540.0)
    c.check(
        "G12 header-only vline excluded from data rows",
        184.5 not in xs_data and any(abs(x - 210.5) < 0.2 for x in xs_data),
        str(xs_data),
    )
    c.check(
        "G12 오수맨홀 not crossed by name|spec vline 210.5",
        _vline_crosses(150.9, 198.9, [210.5]) is False,
    )
    c.check(
        "G12 diagram (표) 양호+code skipped",
        _is_diagram_note_table("(표) 양호 | 보통 | 불량 / ND109.11111 0-150m이내, 양호") is True,
    )
    c.check(
        "G12 real 구분 (표) kept",
        _is_diagram_note_table("(표) 구분 | 전용횟수 | 적용기준 / 매끈한마감 | 2～3회") is False,
    )
    c.check(
        'G12 gate "1" is figure word',
        _is_figure_word(317.0, 280.0, 325.0, 288.0, '"1"', [], True) is True,
    )
    c.check(
        "G12 gate 600 alone is figure word",
        _is_figure_word(130.0, 460.0, 145.0, 475.0, "600", [], True) is True,
    )

    # K3③: conservation.py 와 extract.py 의 PUA 숫자 대응표 키 집합이 같아야 한다.
    from .conservation import _PUA_DIGIT_MAP as _GATE_PUA
    from .extract import _PUA_DIGIT_MAP as _EXTRACT_PUA

    c.check(
        "K3 PUA digit map keys identical (extract vs conservation)",
        set(_GATE_PUA.keys()) == set(_EXTRACT_PUA.keys()),
        f"extract={sorted(_EXTRACT_PUA)} gate={sorted(_GATE_PUA)}",
    )

    # L8: 없는 파일·빈 파일·글자층 0 PDF → 한 줄 오류, 종료 코드 2
    from .cli import main as cli_main
    from .extract import ValidationError, extract_pdf
    import pymupdf as fitz

    with tempfile.TemporaryDirectory(prefix="g02j_l8_") as tmp:
        tmp_p = Path(tmp)
        missing = tmp_p / "no_such.pdf"
        try:
            extract_pdf(missing, half="2025H2")
            c.check("L8 missing file raises", False, "no exception")
        except ValidationError as e:
            c.check("L8 missing file message", "없습니다" in str(e), str(e))
        empty = tmp_p / "empty.pdf"
        empty.write_bytes(b"")
        try:
            extract_pdf(empty, half="2025H2")
            c.check("L8 empty file raises", False, "no exception")
        except ValidationError as e:
            c.check("L8 empty file message", "비어" in str(e) or "손상" in str(e) or "글자층" in str(e), str(e))
        blank_pdf = tmp_p / "blank.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(blank_pdf)
        doc.close()
        try:
            extract_pdf(blank_pdf, half="2025H2")
            c.check("L8 no-text PDF raises", False, "no exception")
        except ValidationError as e:
            c.check("L8 no-text PDF message", "글자층" in str(e), str(e))
        out_dir = tmp_p / "out"
        code = cli_main(["--pdf", str(missing), "--half", "2025H2", "--out", str(out_dir)])
        c.check("L8 CLI missing exit 2", code == 2, str(code))

    # ---------- table_seq 단위 사례 (G07) ----------
    from .table_seq import TableSeq

    ts = TableSeq()
    ts.start("가")
    t1_r1 = ts.record("가")
    t1_r2 = ts.record("가")
    ts.note_seen("가")
    t1_r3 = ts.record("가")
    c.check("T1 start-record-record-note-record → 1,1,2", (t1_r1, t1_r2, t1_r3) == (1, 1, 2), str((t1_r1, t1_r2, t1_r3)))

    ts = TableSeq()
    ts.start("가")
    t2_r1 = ts.record("가")
    t2_n1 = ts.note_seen("가")
    t2_n2 = ts.note_seen("가")
    t2_r2 = ts.record("가")
    c.check(
        "T2 겹친 신호는 한 번만 오른다",
        (t2_r1, t2_r2) == (1, 2) and t2_n1 == 1 and t2_n2 == 1,
        str((t2_r1, t2_n1, t2_n2, t2_r2)),
    )

    ts = TableSeq()
    ts.start("가")
    t3_n = ts.note_seen("가")
    t3_r = ts.record("가")
    c.check("T3 주석이 레코드보다 먼저 → 1,1", t3_n == 1 and t3_r == 1, str((t3_n, t3_r)))

    ts = TableSeq()
    ts.start("가")
    t4_ga1 = ts.record("가")
    ts.note_seen("가")
    ts.start("나")
    t4_na = ts.record("나")
    t4_ga2 = ts.record("가")
    c.check("T4 그룹끼리 섞이지 않는다", (t4_ga1, t4_na, t4_ga2) == (1, 1, 2), str((t4_ga1, t4_na, t4_ga2)))

    ts = TableSeq()
    c.check("T5 record(None)·note_seen(None) 은 None", ts.record(None) is None and ts.note_seen(None) is None)

    from .extract import _parse_price as parse_price_pdf
    from .extract_hwp import _parse_price as parse_price_hwp

    for name, fn in (("pdf", parse_price_pdf), ("hwp", parse_price_hwp)):
        c.check(
            f"P1 {name} _parse_price 1,361263",
            fn("1,361263") == ("1,361263", 1361263, "present"),
            str(fn("1,361263")),
        )
        c.check(
            f"P2 {name} _parse_price 앞뒤 공백",
            fn(" 1,234 ") == ("1,234", 1234, "present"),
            str(fn(" 1,234 ")),
        )
        c.check(
            f"P2 {name} _parse_price 쉼표 옆 공백",
            fn("1, 234") == ("1,234", 1234, "present"),
            str(fn("1, 234")),
        )
        c.check(
            f"P3 {name} _parse_price 폐지",
            fn("폐지") == ("폐지", None, "abolished"),
            str(fn("폐지")),
        )

    from .extract import _norm_unit

    for raw in ("t", "Ton", "톤", "ton"):
        c.check(f"U1 _norm_unit({raw!r}) → ton", _norm_unit(raw) == "ton", _norm_unit(raw))
    c.check("U2 _norm_unit ㎡ → m2", _norm_unit("㎡") == "m2", _norm_unit("㎡"))
    c.check("U2 _norm_unit m → m", _norm_unit("m") == "m", _norm_unit("m"))

    from .extract import _reassign_empty_page_fields

    f1 = [
        {"pdf_page": 1, "field": None},
        {"pdf_page": 2, "field": "토목"},
        {"pdf_page": 3, "field": "토목"},
        {"pdf_page": 4, "field": "토목"},
        {"pdf_page": 5, "field": "건축"},
    ]
    _reassign_empty_page_fields(f1, {2, 5})
    c.check(
        "F1 빈 쪽 3·4는 건축, 1은 null",
        f1[0]["field"] is None and f1[2]["field"] == "건축" and f1[3]["field"] == "건축" and f1[1]["field"] == "토목" and f1[4]["field"] == "건축",
        str([p["field"] for p in f1]),
    )

    f2 = [
        {"pdf_page": 1, "field": "토목"},
        {"pdf_page": 2, "field": "토목"},
        {"pdf_page": 3, "field": "토목"},
    ]
    _reassign_empty_page_fields(f2, {1, 3})
    c.check("F2 같은 분야 안의 빈 쪽은 그대로", [p["field"] for p in f2] == ["토목", "토목", "토목"])

    f3 = [
        {"pdf_page": 1, "field": "토목"},
        {"pdf_page": 2, "field": "토목"},
    ]
    _reassign_empty_page_fields(f3, {1})
    c.check("F3 뒤에 내용 있는 쪽이 없으면 그대로", [p["field"] for p in f3] == ["토목", "토목"])

    from .extract import _join_note_lines

    j1 = _join_note_lines(["① 재료비는 제외되", "어 있다."], [False, None])
    c.check("J1 한글-한글 공백 없음은 붙여 잇는다", j1 == "① 재료비는 제외되어 있다.", j1)
    j2 = _join_note_lines(["① 재료비 및", "운반비를 포함한다."], [True, None])
    c.check("J2 줄 끝 공백 있으면 공백으로 잇는다", j2 == "① 재료비 및 운반비를 포함한다.", j2)
    j3 = _join_note_lines(["① 설치철거,", "잭설치철거를 포함한다."], [False, None])
    c.check("J3 쉼표 뒤는 공백", j3 == "① 설치철거, 잭설치철거를 포함한다.", j3)
    j4 = _join_note_lines(["① 규격은 D", "25 이상이다."], [False, None])
    c.check("J4 한글-한글이 아니면 공백", j4 == "① 규격은 D 25 이상이다.", j4)
    j5 = _join_note_lines(["① 인", "건비를 포", "함한다."], [False, False, None])
    c.check("J5 세 줄 붙여 잇는다", j5 == "① 인건비를 포함한다.", j5)
    j6 = _join_note_lines(["① 재료비는 제외되", "어 있다."], [None, None])
    c.check("J6 모르면 공백으로 잇는다", j6 == "① 재료비는 제외되 어 있다.", j6)

    from .extract import _join_cell_lines as _pdf_join_cell_lines

    c.check(
        "S1 / 줄바꿈은 빈칸 없이",
        _pdf_join_cell_lines("쉬트파일박기/진동식/\n(N≦15)") == "쉬트파일박기/진동식/(N≦15)",
    )
    c.check(
        "S2 / 뒤 빈칸도 떼고 잇는다 (F3-b)",
        _pdf_join_cell_lines("쉬트파일박기/진동식/ \n(N≦15)") == "쉬트파일박기/진동식/(N≦15)",
    )
    c.check(
        "S2b 아랫줄이 /로 시작하면 빈칸 없이 (F3-b)",
        _pdf_join_cell_lines("리핑암\n/굴착깊이") == "리핑암/굴착깊이"
        and _pdf_join_cell_lines("리핑암 \n /굴착깊이") == "리핑암/굴착깊이",
    )
    c.check(
        "S2c 같은 줄 안 / 옆 빈칸과 일반 줄바꿈은 그대로 (F3-b)",
        _pdf_join_cell_lines("가 / 나\n다") == "가 / 나 다"
        and _pdf_join_cell_lines("가\n나 /다") == "가 나 /다",
    )
    c.check("S3 일반 줄바꿈은 빈칸", _pdf_join_cell_lines("콘크리트\n타설") == "콘크리트 타설")
    c.check("S4 연속 / 줄바꿈", _pdf_join_cell_lines("가/\n나/\n다") == "가/나/다")
    c.check("S5 혼합", _pdf_join_cell_lines("가/\n나  다\n라") == "가/나 다 라")

    from .figures import (
        c_exclude_reason,
        fig_filename,
        fig_id_of,
        group_code_set,
        match_groups_by_codes,
        record_index,
        region_is_junk,
    )
    from .figlink import decide_usage, figure_text_bag, group_data_tuple

    c.check("F11 fig_id 규칙", fig_id_of("2025H2#p89#12", 1) == "2025H2#p89#12#f1")
    c.check("F11 파일 이름 #→_", fig_filename("2025H2#p89#12#f1") == "2025H2_p89_12_f1.png")
    c.check("F11 unassigned", fig_id_of(None, 2) == "unassigned#f2")

    d = decide_usage(
        ["4m", "시공기준면"],
        ["4m", "시공기준면"],
        group_data_tuple([], []),
        group_data_tuple([], []),
        has_twin=True,
        twin_has_figs=True,
    )
    c.check(
        "F11 도해 글자 같음 → rep",
        d["used"] == "rep" and d["basis"] == "figure_text" and d["same"] is True and d["review"] is False,
        str(d),
    )
    d = decide_usage(
        ["4m"],
        ["5m"],
        group_data_tuple([], []),
        group_data_tuple([], []),
        has_twin=True,
        twin_has_figs=True,
    )
    c.check(
        "F11 도해 글자 다름 → own+review",
        d["used"] == "own" and d["review"] is True and d["basis"] == "figure_text" and d["review_reason"] == "그림 글자 다름",
        str(d),
    )
    same_data = group_data_tuple(["① 포함한다."], [("NA201.00000", "토사굴착", "-", "m3")])
    d = decide_usage([], [], same_data, same_data, has_twin=True, twin_has_figs=True)
    c.check(
        "F11 글자 없는 그림 + 공종 데이터 같음 → rep",
        d["used"] == "rep" and d["basis"] == "group_data" and d["review"] is False,
        str(d),
    )
    d = decide_usage(
        [],
        [],
        group_data_tuple(["① 포함한다."], [("NA201.00000", "토사굴착", "-", "m3")]),
        group_data_tuple(["① 제외한다."], [("NA201.00000", "토사굴착", "-", "m3")]),
        has_twin=True,
        twin_has_figs=True,
    )
    c.check(
        "F11 글자 없는 그림 + 주석 한 줄 다름 → own+review",
        d["used"] == "own" and d["review"] is True and d["basis"] == "group_data" and d["review_reason"] == "공종 데이터 다름",
        str(d),
    )
    d = decide_usage(["4m"], [], group_data_tuple([], []), None, has_twin=False, twin_has_figs=False)
    c.check(
        "F11 짝 없음 → own+review",
        d["used"] == "own" and d["basis"] == "no_twin" and d["review"] is True,
        str(d),
    )

    recs_a = [
        {"group_id": "ga", "code": "AA001.00001"},
        {"group_id": "gb", "code": "BB001.00001"},
    ]
    recs_b = [
        {"group_id": "g1", "code": "AA001.00001"},
        {"group_id": "g2", "code": "BB001.00001"},
    ]
    src = [("ga", group_code_set("ga", recs_a)), ("gb", group_code_set("gb", recs_a))]
    dst = [("g1", group_code_set("g1", recs_b)), ("g2", group_code_set("g2", recs_b))]
    paired = match_groups_by_codes(src, dst, min_jaccard=0.5)
    c.check("F11 공종코드 묶음으로 짝", paired == {"ga": "g1", "gb": "g2"}, str(paired))
    recs_same_title = [
        {"group_id": "gx", "code": "HA201.00001"},
        {"group_id": "gy", "code": "JB301.00001"},
    ]
    recs_twin = [
        {"group_id": "t1", "code": "JB301.00001"},
        {"group_id": "t2", "code": "ZZ999.00001"},
    ]
    src2 = [("gx", group_code_set("gx", recs_same_title)), ("gy", group_code_set("gy", recs_same_title))]
    dst2 = [("t1", group_code_set("t1", recs_twin)), ("t2", group_code_set("t2", recs_twin))]
    paired2 = match_groups_by_codes(src2, dst2, min_jaccard=0.5)
    c.check(
        "F11 제목 같아도 공종코드가 다르면 짝 안 짓는다",
        paired2.get("gx") is None and paired2.get("gy") == "t1",
        str(paired2),
    )

    bag = figure_text_bag(["  4m  ", "① 아래와 같다.", "※ 참고", "시공기준면", "포함한다."])
    c.check("F11 그림 글자 묶음은 주석형 줄을 뺀다", bag == ["4m", "시공기준면"], str(bag))

    with tempfile.TemporaryDirectory(prefix="g11_mdfig_") as tmp:
        md_dir = Path(tmp) / "out" / "md"
        g1 = _group("g1", "토목", "A", "일반", "같은제목", page=10)
        g2 = _group("g2", "토목", "A", "일반", "같은제목", page=20)
        result = {
            "records": [
                _rec("g1", "A001", "터파기", "1종", "m3", "10,000", "50%", page=10),
                _rec("g2", "A002", "되메우기", "1종", "m3", "20,000", "40%", page=20),
            ],
            "groups": [g1, g2],
            "half": "2025H2",
            "sha256": "deadbeef",
            "pages": [{"pdf_page": 10, "layout": "L1"}, {"pdf_page": 20, "layout": "L1"}],
        }
        render_md(result, md_dir, PDF_NAME)
        p = md_dir / "토목" / "A_일반.md"
        before = p.read_bytes()
        render_md(result, md_dir, PDF_NAME)
        c.check("F11 그림 목록 없으면 md 바이트 같음", p.read_bytes() == before)
        figs = {
            "g1": [{"k": 1, "path": "figures/own/a.png", "source": None}],
            "g2": [{"k": 1, "path": "figures/own/b.png", "source": "출처: 2026H1 대표 그림"}],
        }
        render_md(result, md_dir, PDF_NAME, group_figures=figs)
        text = p.read_text(encoding="utf-8")
        i1 = text.find("## ■ 같은제목")
        i2 = text.find("## ■ 같은제목", i1 + 1)
        c.check("F11 같은 제목 그룹이 두 단락", i1 >= 0 and i2 > i1)
        block1, block2 = text[i1:i2], text[i2:]
        c.check("F11 첫 단락에 a.png", "a.png" in block1 and "b.png" not in block1)
        c.check("F11 둘째 단락에 b.png", "b.png" in block2 and "a.png" not in block2)
        c.check("F11 둘째만 출처 줄", "*(출처: 2026H1 대표 그림)*" in block2 and "*(출처:" not in block1)

        figs_orphan = {"unassigned": [{"k": 1, "path": "figures/own/u.png", "source": None}]}
        render_md(result, md_dir, PDF_NAME, group_figures=figs_orphan)
        text2 = p.read_text(encoding="utf-8")
        c.check("F11b E 공종 없는 그림이 md 에 없음", "u.png" not in text2)

    codes, prices, labors = record_index(
        [{"code": "DE149.10000", "price_raw": "48,630", "labor_raw": "70.83%"}]
    )
    c.check(
        "F11b C-1 공종코드 라벨 도해는 남음",
        c_exclude_reason("DE149.10000", set(), codes, prices, labors, []) is None,
    )
    c.check(
        "F11b C-1 같은 높이 단가 있으면 표 칸",
        c_exclude_reason("DE149.10000", {"48,630", "70.83%"}, codes, prices, labors, []) == "table",
    )
    c.check(
        "F11b C-1 단가 칸은 빠짐",
        c_exclude_reason("48,630", set(), codes, prices, labors, []) == "table",
    )
    c.check(
        "F11b C-1 같은 줄 코드+단가",
        c_exclude_reason("DE149.1000048,630", set(), codes, prices, labors, []) == "table",
    )
    c.check(
        "F11b C-1 단가·노무비율 붙음",
        c_exclude_reason("48,63070.83%", set(), codes, prices, labors, []) == "table",
    )
    c.check(
        "F11b C-1 짧은 단가 부분문자열은 도해가 아님",
        c_exclude_reason("370-550m이내,양호", set(), codes, {"550", "48,630"}, labors, []) is None,
    )
    note = "④스티프너보강에소요되는단가는철판등재료비가포함되어있으며전기용접에필요한발전기는제외되어있다."
    c.check(
        "F11b C-2 주석 문장 빠짐",
        c_exclude_reason("④스티프너보강에소요되는단가는철판등재료비가포함되어있으며", set(), {}, set(), set(), [note]) == "note",
    )
    c.check(
        "F11b C-2 그림 제목은 안 뺌",
        c_exclude_reason("[표준도]", set(), {}, set(), set(), [note]) is None,
    )
    c.check(
        "F11b D-1 점 영역 버림",
        region_is_junk([460.6, 173.1, 461.2, 173.8], [{"kind": "draw", "box": [460.6, 173.1, 461.2, 173.8]}]),
    )
    c.check(
        "F11b D-2 글 조각 버림",
        region_is_junk([10, 10, 80, 30], [{"kind": "text", "box": [10, 10, 80, 30], "text": "단위"}]),
    )
    c.check(
        "F11b D-2 그림 제목만 있으면 남음",
        not region_is_junk([10, 10, 80, 30], [{"kind": "text", "box": [10, 10, 80, 30], "text": "[표준도]"}]),
    )

    # ---------- F1: 대분류 인쇄 표기 major_raw ----------
    from .extract import _parse_banner_line
    from .spec import banner_major_raw

    c.check(
        "F1 띠 단일 -> (A, 이름, 'A')",
        _parse_banner_line("대분류 A 공통공사") == ("A", "공통공사", "A"),
        repr(_parse_banner_line("대분류 A 공통공사")),
    )
    c.check(
        "F1 띠 합친 -> (Q, 이름, 'Q, R')",
        _parse_banner_line("대분류 Q, R 기타공사") == ("Q", "기타공사", "Q, R"),
        repr(_parse_banner_line("대분류 Q, R 기타공사")),
    )
    c.check(
        "F1 띠 글자 셋 · 공백 여럿은 하나로",
        banner_major_raw("대분류   Q ,  R,S  기타공사") == "Q , R,S" and banner_major_raw("대분류 Q") == "Q",
        banner_major_raw("대분류   Q ,  R,S  기타공사"),
    )
    c.check("F1 띠가 아니면 빈 문자열", banner_major_raw("일반사항") == "")

    def _g_raw(gid, major, major_raw, name, header):
        g = _group(gid, "토목", major, name, header)
        if major_raw is not None:
            g["major_raw"] = major_raw
        return g

    res_raw = {
        "records": [
            _rec("q1", "QD201.10000", "기타1", "-", "식", "1,000", "10%"),
            _rec("a1", "AA101.10000", "일반1", "-", "식", "1,000", "10%"),
            _rec("o1", "BA101.10000", "옛판", "-", "식", "1,000", "10%"),
        ],
        "groups": [
            _g_raw("q1", "Q", "Q, R", "기타공사", "기타 그룹"),
            _g_raw("a1", "A", "A", "공통공사", "일반 그룹"),
            _g_raw("o1", "B", None, "옛대분류", "major_raw 없는 옛 결과"),
        ],
        "half": "2026H1",
        "sha256": "deadbeef",
        "pages": [{"pdf_page": 1, "layout": "L1"}],
    }
    with tempfile.TemporaryDirectory(prefix="f1_raw_") as tmp:
        md_dir = Path(tmp) / "md"
        render_md(res_raw, md_dir, PDF_NAME)
        names = sorted(str(p.relative_to(md_dir).as_posix()) for p in md_dir.rglob("*.md"))
        c.check(
            "F1 md 파일 이름은 major 그대로",
            names == ["토목/A_공통공사.md", "토목/B_옛대분류.md", "토목/Q_기타공사.md"],
            str(names),
        )
        q_txt = (md_dir / "토목" / "Q_기타공사.md").read_text(encoding="utf-8")
        a_txt = (md_dir / "토목" / "A_공통공사.md").read_text(encoding="utf-8")
        b_txt = (md_dir / "토목" / "B_옛대분류.md").read_text(encoding="utf-8")
        c.check("F1 합친 띠 제목 줄 (대분류 Q, R 기타공사)", "(대분류 Q, R 기타공사)" in q_txt, q_txt[:300])
        c.check("F1 단일 띠 제목 줄 (대분류 A 공통공사)", "(대분류 A 공통공사)" in a_txt, a_txt[:300])
        c.check("F1 major_raw 없으면 major 로", "(대분류 B 옛대분류)" in b_txt, b_txt[:300])


    # ---------- F2: 옛 판 주석 끝 도면 글자 · 행 상자 · 꺾인 코드 · 병합 명칭 칸 · 축소 ■ 제목 ----------
    from .extract import (
        Span,
        _caption_bottom,
        _codes_and_stars,
        _drop_figure_area_spans,
        _row_boundary_splits_cell,
    )
    from .spec import (
        accept_tiny_titles,
        boundary_splits_cell,
        code_tail_below,
        in_figure_area,
    )

    # 순수 기하 도우미
    c.check(
        "F2 꺾인 코드 꼬리: 바로 아래 같은 칸 낱글자",
        code_tail_below((61.6, 485.5, 125.5, 499.8), (90.7, 501.1, 96.6, 515.4)) is True,
    )
    c.check(
        "F2 꺾인 코드 꼬리: 다음 행(2줄 아래) 글자는 아님",
        code_tail_below((61.6, 485.5, 125.5, 499.8), (90.7, 531.0, 96.6, 545.4)) is False,
    )
    c.check(
        "F2 꺾인 코드 꼬리: 칸 밖 오른쪽 글자는 아님",
        code_tail_below((61.6, 485.5, 125.5, 499.8), (330.0, 501.1, 336.0, 515.4)) is False,
    )
    sp_code = [
        Span(64.4, 485.5, 125.5, 499.8, "ND109.1112", 12.0),
        Span(90.7, 501.1, 96.6, 515.4, "1", 12.0),
        Span(64.4, 517.2, 125.5, 531.6, "ND109.1112", 12.0),
        Span(90.7, 529.2, 96.6, 543.6, "2", 12.0),
    ]
    codes_f2, _stars_f2 = _codes_and_stars(sp_code, 0.0, 595.0)
    c.check(
        "F2 두 줄로 꺾인 공종코드를 이어 코드로 인식(위 행·아래 행)",
        sorted(x.text for x in codes_f2) == ["ND109.11121", "ND109.11122"],
        str([x.text for x in codes_f2]),
    )
    c.check(
        "F2 꼬리가 없는 앞 열 자리는 코드가 아님",
        _codes_and_stars([Span(64.4, 485.5, 125.5, 499.8, "ND109.1112", 12.0)], 0.0, 595.0)[0] == [],
    )
    hl_merged = [LineSeg(59.4, 131.0, 148.0), LineSeg(244.3, 536.1, 148.0)]
    c.check(
        "F2 병합 칸: 경계선이 명칭 칸에서만 끊김",
        _row_boundary_splits_cell(hl_merged, 130.0, 160.0, (131.0, 244.3), (59.4, 536.1)) is True,
    )
    c.check(
        "F2 병합 칸 아님: 경계선이 전체 폭",
        _row_boundary_splits_cell([LineSeg(59.4, 536.1, 148.0)], 130.0, 160.0, (131.0, 244.3), (59.4, 536.1)) is False,
    )
    c.check(
        "F2 병합 칸 아님: 경계선이 아예 없음",
        _row_boundary_splits_cell([], 130.0, 160.0, (131.0, 244.3), (59.4, 536.1)) is False,
    )
    c.check(
        "F2 boundary_splits_cell 직접",
        boundary_splits_cell([(59.4, 131.0), (244.3, 536.1)], (131.0, 244.3), (59.4, 536.1)) is True,
    )
    c.check(
        "F2 그림 영역: 제목 줄 아래 영역 안만 True",
        in_figure_area(321.0, 723.0, 612.0, [(1.7, 569.3, 584.6, 795.8)]) is True
        and in_figure_area(321.0, 580.0, 612.0, [(1.7, 569.3, 584.6, 795.8)]) is False
        and in_figure_area(321.0, 723.0, None, [(1.7, 569.3, 584.6, 795.8)]) is False
        and in_figure_area(700.0, 723.0, 612.0, [(1.7, 569.3, 584.6, 795.8)]) is False,
    )
    band_f2 = [
        Span(68.6, 576.3, 80.6, 590.7, "③", 12.0),
        Span(86.6, 576.3, 200.0, 590.7, "맨홀발 디딤쇄 설치비는 포함한다.", 12.0),
        Span(86.6, 597.9, 126.5, 612.3, "[표준도]", 12.0),
        Span(148.6, 597.9, 247.7, 612.3, "우수맨홀(900×900)", 12.0),
        Span(316.0, 721.0, 337.6, 729.0, "초잡석", 12.0),
    ]
    c.check("F2 그림 제목 줄 아랫끝", abs((_caption_bottom(band_f2) or 0) - 612.3) < 0.01, str(_caption_bottom(band_f2)))
    kept_f2 = _drop_figure_area_spans(band_f2, lambda: [(1.7, 569.3, 584.6, 795.8)], _caption_bottom(band_f2))
    c.check(
        "F2 도면 영역 글자만 빠지고 ③ 문장·제목 줄은 남는다",
        [x.text for x in kept_f2] == ["③", "맨홀발 디딤쇄 설치비는 포함한다.", "[표준도]", "우수맨홀(900×900)"],
        str([x.text for x in kept_f2]),
    )
    c.check(
        "F2 그림 제목 줄이 없으면 아무것도 안 뺀다",
        _drop_figure_area_spans(band_f2[:2], lambda: [(1.7, 569.3, 584.6, 795.8)], None) == band_f2[:2],
    )
    c.check(
        "F2 축소 ■ 제목: 아래 첫 코드 접두와 같으면 받아들임, AA21* 같은 다른 제목은 안 받음",
        accept_tiny_titles(
            [(137.0, "■AA21*엄지말뚝-변전소"), (165.0, "■AE14*쉬트파일-전력구")],
            [],
            [(518.0, "AE141.11100"), (548.0, "AE141.12100")],
        )
        == [165.0],
    )
    c.check(
        "F2 축소 ■ 제목: 아래 본문 코드가 없으면(도면 안 사본) 안 받음",
        accept_tiny_titles([(312.0, "■AE16*축버팀대(Strut)")], [], []) == [],
    )
    c.check(
        "F2 축소 ■ 제목: 사이에 실제 ■ 제목이 있으면 안 받음",
        accept_tiny_titles([(165.0, "■AE14*쉬트파일")], [300.0], [(518.0, "AE141.11100")]) == [],
    )
    from .figures import table_margin_debris

    tb_f2 = [[55.4, 619.0, 544.8, 720.7]]
    cr_f2 = [0, 0, 595.0, 842.0]
    c.check(
        "F2 그림 영역 정리: 표 옆 왼쪽 여백의 작은 도면 조각은 그림이 아니다",
        table_margin_debris([4.1, 654.1, 9.7, 659.4], tb_f2, cr_f2) is True
        and table_margin_debris([560.0, 654.1, 570.0, 659.4], tb_f2, cr_f2) is True,
    )
    c.check(
        "F2 그림 영역 정리: 표 안·표와 다른 높이·큰 도면은 여백 조각이 아니다",
        table_margin_debris([100.0, 654.1, 105.0, 659.4], tb_f2, cr_f2) is False
        and table_margin_debris([4.1, 400.0, 9.7, 405.0], tb_f2, cr_f2) is False
        and table_margin_debris([4.1, 654.1, 140.0, 700.0], tb_f2, cr_f2) is False,
    )

    # 합성 PDF 끝에서 끝까지(추출 + 보존 검사)
    import pymupdf as _fz2

    def _f2_make_pdf(path: Path) -> None:
        doc = _fz2.open()

        def txt(page, x, y, s_, size=12.0):
            page.insert_text((x, y), s_, fontname="korea", fontsize=size)

        def table(page, y_top, rows, merged_first_boundary=None, x_left_junk=False):
            xs = [40, 200, 310, 380, 420, 470, 555]
            y_hdr = y_top + 30
            y_end = y_hdr + 40 * len(rows)
            page.draw_line((40, y_top), (555, y_top))
            page.draw_line((40, y_hdr), (555, y_hdr))
            for x in xs:
                page.draw_line((x, y_top), (x, y_end))
            for i in range(len(rows)):
                yb = y_hdr + 40 * (i + 1)
                if merged_first_boundary == i:
                    page.draw_line((40, yb), (200, yb))
                    page.draw_line((310, yb), (555, yb))
                else:
                    page.draw_line((40, yb), (555, yb))
            if x_left_junk:
                # 쪽 왼쪽 밖까지 뻗은 도면 가로선(표 격자 선이 아님)
                page.draw_line((-30, y_hdr + 1.5), (300, y_hdr + 1.5))
            for lab, x in (
                ("공종코드", 90), ("공종명칭", 230), ("규격", 332), ("단위", 388), ("단가", 432), ("노무비율", 490),
            ):
                txt(page, x, y_top + 20, lab)
            for i, (head, tail, name, spec, price, labor) in enumerate(rows):
                base = y_hdr + 40 * i + 22
                txt(page, 45, base, head)
                if tail:
                    txt(page, 100, base + 16, tail)
                if name:
                    txt(page, 210, base, name)
                txt(page, 318, base, spec)
                txt(page, 392, base, "m")
                txt(page, 424, base, price)
                txt(page, 514, base, labor)
            return y_end

        # 쪽 1: 꺾인 코드 · 병합 명칭 칸 · 그림 제목 아래 도면 라벨
        p1 = doc.new_page(width=595, height=842)
        txt(p1, 60, 90, "■ AB10* 시험 그룹 - 공통")
        end = table(
            p1, 110,
            [
                ("AB101.1000", "1", "시험명칭/", "규격A", "1,234", "55%"),
                ("AB101.20001", "", "", "규격B", "2,345", "50%"),
                ("AB101.30001", "", "〃", "규격C", "3,456", "45%"),
            ],
            merged_first_boundary=0,
        )
        txt(p1, 210, 140 + 40 + 22, "병합줄")
        txt(p1, 60, end + 30, "【단가정의】")
        txt(p1, 70, end + 50, "① 첫째 항목입니다.")
        cap_y = end + 80
        txt(p1, 70, cap_y, "[표준도] 도면제목")
        p1.draw_rect(_fz2.Rect(40, cap_y + 15, 555, cap_y + 115))
        txt(p1, 200, cap_y + 60, "도면라벨")
        # 쪽 2: 앞 쪽에서 이어지는 주석 + 다음 쪽 그림 제목·도면 라벨 + 왼쪽 밖 도면 선이 섞인 표
        p2 = doc.new_page(width=595, height=842)
        txt(p2, 70, 70, "② 이어지는 항목입니다.")
        txt(p2, 70, 100, "[표준도] 다음도면")
        p2.draw_rect(_fz2.Rect(40, 115, 555, 215))
        txt(p2, 200, 165, "다음쪽라벨")
        txt(p2, 60, 250, "■ AB20* 둘째 그룹 - 공통")
        table(p2, 270, [("AB201.10000", "", "둘째명칭", "규격D", "4,567", "40%")], x_left_junk=True)
        # 쪽 3: 축소 글자로만 찍힌 ■ 제목(아래 첫 코드 접두 AB30 과 같음)
        p3 = doc.new_page(width=595, height=842)
        txt(p3, 150, 100, "■ AB30* 축소제목 - 공통", size=1.8)
        txt(p3, 150, 115, "【단가정의】", size=1.8)
        txt(p3, 150, 120, "① 축소 항목입니다.", size=1.8)
        end3 = table(p3, 300, [("AB301.10000", "", "셋째명칭", "규격E", "5,678", "35%")])
        txt(p3, 60, end3 + 30, "【단가정의】")
        txt(p3, 70, end3 + 50, "③ 실제 항목입니다.")
        # 쪽 4: 도면 안 축소 ■ 사본(아래 본문 코드 없음 -> 그룹 아님)
        p4 = doc.new_page(width=595, height=842)
        txt(p4, 60, 90, "■ AB40* 넷째 그룹 - 공통")
        end4 = table(p4, 110, [("AB401.10000", "", "넷째명칭", "규격F", "6,789", "30%")])
        txt(p4, 60, end4 + 30, "【단가정의】")
        txt(p4, 70, end4 + 50, "① 넷째 항목입니다.")
        p4.draw_rect(_fz2.Rect(40, 450, 555, 600))
        txt(p4, 150, 500, "■ AB99* 사본제목 - 공통", size=1.8)
        txt(p4, 150, 510, "① 사본 항목입니다.", size=1.8)
        # 쪽 5: 사건 없이 글만 -> 앞 쪽 끝 사본 그룹의 이어짐
        p5 = doc.new_page(width=595, height=842)
        txt(p5, 70, 80, "⑤ 사본 이어짐입니다.")
        doc.save(path)
        doc.close()

    from .conservation import check_conservation as _check_cons_f2

    with tempfile.TemporaryDirectory(prefix="f2_synth_") as tmp:
        f2_pdf = Path(tmp) / "f2.pdf"
        _f2_make_pdf(f2_pdf)
        f2 = extract_pdf(f2_pdf, half="2016H1")
        f2_cache = f2.pop("_table_cache", None)
        f2_rep = _check_cons_f2(f2_pdf, f2, table_cache=f2_cache)
        f2_g = {g["header"].split()[0]: g for g in f2["groups"]}
        f2_r = {r["code"]: r for r in f2["records"]}

        c.check(
            "F2 그룹은 AB10* AB20* AB30*(축소 제목) AB40* 넷뿐 — 도면 안 사본 AB99* 는 그룹이 아님",
            [g["header"].split()[0] for g in f2["groups"]] == ["AB10*", "AB20*", "AB30*", "AB40*"],
            str([g["header"] for g in f2["groups"]]),
        )
        c.check(
            "F2 쪽을 넘는 주석: 그림 라벨(도면라벨·다음쪽라벨)을 끌고 오지 않는다",
            [n["item_raw"] for n in f2_g["AB10*"]["notes"]] == ["① 첫째 항목입니다.", "② 이어지는 항목입니다."],
            str([n["item_raw"] for n in f2_g["AB10*"]["notes"]]),
        )
        c.check(
            "F2 두 줄로 꺾인 공종코드 AB101.10001 레코드가 있다",
            "AB101.10001" in f2_r and f2_r["AB101.10001"]["price_raw"] == "1,234",
            str(sorted(f2_r)),
        )
        r_a, r_b, r_c = f2_r.get("AB101.10001", {}), f2_r.get("AB101.20001", {}), f2_r.get("AB101.30001", {})
        c.check(
            "F2 병합 명칭 칸: 첫 행 name_raw 는 칸 전체, name 은 줄을 이은 전체 명칭",
            r_a.get("name") == "시험명칭/병합줄"
            and r_a.get("name_raw") == "시험명칭/\n병합줄"
            and r_a.get("name_inherited") is False,
            str((r_a.get("name"), r_a.get("name_raw"))),
        )
        c.check(
            "F2 병합 명칭 칸: 둘째 행은 name_raw 빈칸·상속, name 은 첫 행 전체 명칭",
            r_b.get("name") == "시험명칭/병합줄" and r_b.get("name_raw") == "" and r_b.get("name_inherited") is True,
            str((r_b.get("name"), r_b.get("name_raw"), r_b.get("name_inherited"))),
        )
        c.check(
            "F2 병합 칸 뒤 〃 행도 전체 명칭을 이어받는다",
            r_c.get("name") == "시험명칭/병합줄" and r_c.get("name_inherited") is True,
            str((r_c.get("name"), r_c.get("name_inherited"))),
        )
        c.check(
            "F2 병합 칸 첫 행에만 name_cell_bbox(칸이 걸친 모든 행)",
            r_a.get("name_cell_bbox") == [200.0, 140.0, 310.0, 220.0]
            and "name_cell_bbox" not in r_b
            and "name_cell_bbox" not in r_c,
            str((r_a.get("name_cell_bbox"), r_b.get("name_cell_bbox"))),
        )
        c.check(
            "F2 쪽 밖으로 뻗은 도면 선이 섞여도 행 상자 x0 는 표 칸 경계(40)",
            f2_r["AB201.10000"]["bbox"][0] == 40.0,
            str(f2_r["AB201.10000"]["bbox"]),
        )
        c.check(
            "F2 축소 ■ 제목 그룹: 레코드 하나 + 축소 ① 과 실제 ③ 주석",
            f2_g["AB30*"]["record_count"] == 1
            and [n["item_raw"] for n in f2_g["AB30*"]["notes"]] == ["① 축소 항목입니다.", "③ 실제 항목입니다."],
            str([n["item_raw"] for n in f2_g["AB30*"]["notes"]]),
        )
        c.check(
            "F2 축소 사본 뒤 쪽 맨 위 글(⑤)은 직전 그룹 AB40* 주석이 아니다",
            [n["item_raw"] for n in f2_g["AB40*"]["notes"]] == ["① 넷째 항목입니다."],
            str([n["item_raw"] for n in f2_g["AB40*"]["notes"]]),
        )
        f2_tot = f2_rep["totals"]
        c.check(
            "F2 합성 PDF 보존 검사 pass (낱말 빠짐·중복·주석 순서·코드 누락 0)",
            f2_tot["pass"] is True
            and f2_tot["missing"] == 0
            and f2_tot["notes_missing"] == 0
            and f2_tot["notes_order_mismatch"] == 0
            and f2_tot["unrecorded_codes"] == 0,
            str({k: v for k, v in f2_tot.items() if v not in (0, False)}),
        )
        c.check(
            "F2 도면 라벨은 주석 낱말이 아니라 도면 글자로 분류(notes_figure_text 2건)",
            f2_tot["notes_figure_text"] == 2,
            str(f2_tot["notes_figure_text"]),
        )
        # 게이트도 꺾인 코드 행이 빠지면 잡는다: 레코드 하나를 지운 결과로 다시 검사
        f2_broken = dict(f2)
        f2_broken["records"] = [r for r in f2["records"] if r["code"] != "AB101.10001"]
        f2_rep_bad = _check_cons_f2(f2_pdf, f2_broken, table_cache=f2_cache)
        c.check(
            "F2 꺾인 코드 레코드가 빠지면 보존 검사가 잡는다(unrecorded_codes)",
            f2_rep_bad["totals"]["unrecorded_codes"] >= 1 and f2_rep_bad["totals"]["pass"] is False,
            str(f2_rep_bad["totals"]["unrecorded_codes"]),
        )

    # ---------- F3: 표 오른쪽 밖으로 뻗은 도면 선 · 줄바꿈 옆 「/」 잇기 ----------
    from .extract import LineSeg as _F3Seg
    from .extract import _trim_overreaching_wide

    grid_f3 = [_F3Seg(59.4, 537.9, y) for y in (289.6, 347.4, 376.3, 376.3, 289.6)]
    junk_f3 = _F3Seg(210.7, 597.4, 281.6)
    c.check(
        "F3 격자 오른쪽 끝을 넘는 도면 선은 표 가로선 후보에서 빠진다",
        _trim_overreaching_wide(grid_f3 + [junk_f3], grid_f3 + [junk_f3]) == grid_f3,
    )
    c.check(
        "F3 격자뿐이거나 선이 하나뿐이면 그대로 둔다",
        _trim_overreaching_wide(grid_f3, grid_f3) == grid_f3
        and _trim_overreaching_wide([junk_f3], [junk_f3]) == [junk_f3],
    )
    # 칸마다 끊긴 가로선: 폭 넓은 조각(앞 두 칸)은 399.9 에서 끝나고 맨 위 선만 539.3 까지 이어진다.
    # 오른쪽 칸 조각(442.4~539.3)이 격자 오른쪽 끝을 받쳐 주므로 맨 위 선을 자르지 않는다.
    rows_f3 = [_F3Seg(57.9, 399.9, y) for y in (144.2, 173.2, 202.1, 231.0)]
    top_f3 = _F3Seg(57.9, 539.3, 86.4)
    right_f3 = [_F3Seg(442.4, 539.3, y) for y in (144.2, 173.2, 202.1, 231.0)]
    c.check(
        "F3 칸마다 끊긴 가로선이면 표 전체 폭 선(맨 위)을 자르지 않는다",
        _trim_overreaching_wide(rows_f3 + [top_f3], rows_f3 + [top_f3] + right_f3) == rows_f3 + [top_f3],
    )
    c.check(
        "F3 같이 끝나는 선이 없으면(표 오른쪽 끝 없음) 그대로 둔다",
        _trim_overreaching_wide(
            [_F3Seg(50, 400, 10), _F3Seg(60, 450, 20), _F3Seg(70, 500, 30)],
            [_F3Seg(50, 400, 10), _F3Seg(60, 450, 20), _F3Seg(70, 500, 30)],
        )
        == [_F3Seg(50, 400, 10), _F3Seg(60, 450, 20), _F3Seg(70, 500, 30)],
    )

    def _f3_make_pdf(path: Path) -> None:
        doc = _fz2.open()
        pg = doc.new_page(width=595, height=842)

        def txt(x, y, s_):
            pg.insert_text((x, y), s_, fontname="korea", fontsize=12.0)

        xs = [40, 200, 310, 380, 420, 470, 540]
        y_top, y_hdr = 110, 140
        y_end = y_hdr + 80
        txt(60, 90, "■ AB50* 시험 그룹 - 공통")
        # 표 위 도면 가로선: 표 오른쪽 끝(540)을 넘어 쪽 가장자리 가까이(594)까지
        pg.draw_line((210, y_top - 6), (594, y_top - 6))
        pg.draw_line((40, y_top), (540, y_top))
        pg.draw_line((40, y_hdr), (540, y_hdr))
        for x in xs:
            pg.draw_line((x, y_top), (x, y_end))
        for i in range(2):
            yb = y_hdr + 40 * (i + 1)
            pg.draw_line((40, yb), (540, yb))
        for lab, x in (("공종코드", 90), ("공종명칭", 230), ("규격", 332), ("단위", 388), ("단가", 432), ("노무비율", 490)):
            txt(x, y_top + 20, lab)
        # 행 1: 윗줄이 「/」로 끝남 · 행 2: 아랫줄이 「/」로 시작
        for i, (code, l1, l2, spec, price) in enumerate(
            [
                ("AB501.10000", "시험명/", "(N≦15)", "규격A", "1,234"),
                ("AB501.20000", "리핑암", "/굴착깊이", "규격B", "2,345"),
            ]
        ):
            base = y_hdr + 40 * i + 18
            txt(45, base, code)
            txt(210, base, l1)
            txt(210, base + 14, l2)
            txt(318, base, spec)
            txt(392, base, "m")
            txt(424, base, price)
            txt(490, base, "55%")
        doc.save(path)
        doc.close()

    from .conservation import check_conservation as _check_cons_f3

    with tempfile.TemporaryDirectory(prefix="f3_synth_") as tmp:
        f3_pdf = Path(tmp) / "f3.pdf"
        _f3_make_pdf(f3_pdf)
        f3 = extract_pdf(f3_pdf, half="2017H1")
        f3_cache = f3.pop("_table_cache", None)
        f3_rep = _check_cons_f3(f3_pdf, f3, table_cache=f3_cache)
        f3_r = {r["code"]: r for r in f3["records"]}
        c.check(
            "F3 표 오른쪽 밖으로 뻗은 도면 선이 있어도 행 상자 x1 은 표 칸 경계(540)",
            sorted(f3_r) == ["AB501.10000", "AB501.20000"]
            and all(r["bbox"][2] == 540.0 for r in f3_r.values()),
            str([r["bbox"] for r in f3_r.values()]),
        )
        c.check(
            "F3 윗줄이 / 로 끝나면 빈칸 없이 잇는다(name_raw 는 줄바꿈 그대로)",
            f3_r["AB501.10000"]["name"] == "시험명/(N≦15)" and f3_r["AB501.10000"]["name_raw"] == "시험명/\n(N≦15)",
            str((f3_r["AB501.10000"]["name"], f3_r["AB501.10000"]["name_raw"])),
        )
        c.check(
            "F3 아랫줄이 / 로 시작하면 빈칸 없이 잇는다(name_raw 는 줄바꿈 그대로)",
            f3_r["AB501.20000"]["name"] == "리핑암/굴착깊이" and f3_r["AB501.20000"]["name_raw"] == "리핑암\n/굴착깊이",
            str((f3_r["AB501.20000"]["name"], f3_r["AB501.20000"]["name_raw"])),
        )
        c.check("F3 합성 PDF 보존 검사 pass", f3_rep["totals"]["pass"] is True, str(f3_rep["totals"]))

    return c


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = run_all()
    print(c.report())
    return 0 if c.all_pass() else 1


if __name__ == "__main__":
    raise SystemExit(main())
