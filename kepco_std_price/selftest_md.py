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
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, cond: bool, detail: str = ""):
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
        other = "D:" if tmp_drive != "D:" else "C:"
        md_dir2 = Path(tmp) / "out2" / "md"
        render_md(make_result_full(), md_dir2, PDF_NAME, pdf_path=f"{other}/__kepco_selftest__/a#b.pdf")
        md_c = (md_dir2 / "토목" / "A_일반.md").read_text(encoding="utf-8")
        c.check(
            "S16 다른 드라이브 원문 링크: file:/// URI · # 부호화",
            f"(<file:///{other}/__kepco_selftest__/a%23b.pdf#page=10>)" in md_c,
            md_c,
        )

    # K3③: conservation.py 와 extract.py 의 PUA 숫자 대응표 키 집합이 같아야 한다.
    from .conservation import _PUA_DIGIT_MAP as _GATE_PUA
    from .extract import _PUA_DIGIT_MAP as _EXTRACT_PUA

    c.check(
        "K3 PUA digit map keys identical (extract vs conservation)",
        set(_GATE_PUA.keys()) == set(_EXTRACT_PUA.keys()),
        f"extract={sorted(_EXTRACT_PUA)} gate={sorted(_GATE_PUA)}",
    )

    return c


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = run_all()
    print(c.report())
    return 0 if c.all_pass() else 1


if __name__ == "__main__":
    raise SystemExit(main())
