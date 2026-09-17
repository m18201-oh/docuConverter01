from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .conservation import check_conservation
from .extract import extract_pdf
from .render_md import MdOutputError, render_md


def _parse_pages(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    s = s.strip()
    if "-" in s:
        a, b = s.split("-", 1)
        return int(a), int(b)
    n = int(s)
    return n, n


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="kepco_std_price")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--half", required=True, help="예: 2025H2")
    ap.add_argument("--pages", default=None, help="예: 46-95 (없으면 전체)")
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--check-conservation",
        action="store_true",
        help="표 본문 낱말이 레코드 필드에 빠짐·중복 없이 들어갔는지 검사",
    )
    args = ap.parse_args(argv)

    pages = _parse_pages(args.pages)
    result = extract_pdf(args.pdf, half=args.half, pages=pages)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out / "records.jsonl", result["records"])
    _write_jsonl(out / "groups.jsonl", result["groups"])
    _write_jsonl(out / "subheaders.jsonl", result["subheaders"])
    _write_jsonl(out / "pages.jsonl", result["pages"])
    try:
        md_summary = render_md(result, out / "md", pdf_name=Path(args.pdf).name)
    except MdOutputError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    for w in md_summary.warnings:
        print(f"경고: {w}", file=sys.stderr)
    print(md_summary.line(), flush=True)
    recs = result["records"]
    n_null = sum(1 for r in recs if not r.get("group_id"))
    n_pres = sum(1 for r in recs if r.get("status") == "present")
    n_ab = sum(1 for r in recs if r.get("status") == "abolished")
    print(
        f"records={len(recs)} present={n_pres} abolished={n_ab} group_id_null={n_null}",
        flush=True,
    )
    if args.check_conservation:
        report = check_conservation(args.pdf, result, pages=pages)
        (out / "conservation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tot = report.get("totals") or {}
        print(
            f"conservation: body={tot.get('body_words')} assigned={tot.get('assigned')} "
            f"missing={tot.get('missing')} duplicate={tot.get('duplicate')} "
            f"split_words={tot.get('split_words')} "
            f"lost_spaces={tot.get('lost_spaces')} "
            f"inserted_spaces={tot.get('inserted_spaces')} "
            f"unrecorded_codes={tot.get('unrecorded_codes')} "
            f"pass={tot.get('pass')}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    main()
