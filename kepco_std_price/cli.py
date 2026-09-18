from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .conservation import check_conservation
from .extract import ValidationError, extract_pdf
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
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pdf", default=None)
    src.add_argument("--hwp", default=None)
    ap.add_argument("--half", required=True, help="예: 2025H2")
    ap.add_argument("--pages", default=None, help="예: 46-95 (없으면 전체, --hwp 와는 함께 쓰지 않음)")
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--check-conservation",
        action="store_true",
        help="표 본문 낱말이 레코드 필드에 빠짐·중복 없이 들어갔는지 검사",
    )
    args = ap.parse_args(argv)

    if args.hwp and args.pages:
        print("오류: --hwp 와 --pages 는 함께 쓸 수 없습니다.", file=sys.stderr)
        return 2

    try:
        pages = _parse_pages(args.pages)
    except ValueError as e:
        print(f"오류: --pages 값을 읽을 수 없습니다: {args.pages!r} ({e})", file=sys.stderr)
        return 2

    # H3·H5: --pages 역전/0/쪽수 초과, --half 형식 오류는 조용히 넘어가지 않고 여기서 멈춘다.
    source_path = Path(args.hwp) if args.hwp else Path(args.pdf)
    try:
        if args.hwp:
            try:
                from hwp5.xmlmodel import Hwp5File  # noqa: F401
            except ImportError:
                print("uv sync --extra hwp 필요", file=sys.stderr)
                return 2
            from .extract_hwp import extract_hwp

            result = extract_hwp(args.hwp, half=args.half)
        else:
            result = extract_pdf(args.pdf, half=args.half, pages=pages)
    except ValidationError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2

    for w in result.get("warnings") or []:
        print(f"경고: {w}", file=sys.stderr)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out / "records.jsonl", result["records"])
    _write_jsonl(out / "groups.jsonl", result["groups"])
    _write_jsonl(out / "subheaders.jsonl", result["subheaders"])
    _write_jsonl(out / "pages.jsonl", result["pages"])
    try:
        md_summary = render_md(
            result, out / "md", pdf_name=source_path.name, pdf_path=source_path
        )
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

    # H4: 게이트 실패·레코드 0을 조용히 0으로 끝내지 않는다.
    exit_code = 0
    if len(recs) == 0:
        print("오류: 추출된 레코드가 0건입니다.", file=sys.stderr)
        exit_code = 1

    table_cache = result.pop("_table_cache", None)
    if args.check_conservation:
        if args.hwp:
            from .conservation_hwp import check_conservation as check_conservation_hwp

            report = check_conservation_hwp(args.hwp, result)
        else:
            report = check_conservation(args.pdf, result, pages=pages, table_cache=table_cache)
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
            f"empty_name={tot.get('empty_name')} "
            f"unresolved_inherit={tot.get('unresolved_inherit')} "
            f"parse_mismatch={tot.get('parse_mismatch')} "
            f"inherit_mismatch={tot.get('inherit_mismatch')} "
            f"notes_missing={tot.get('notes_missing')} "
            f"notes_duplicate={tot.get('notes_duplicate')} "
            f"notes_figure_text={tot.get('notes_figure_text')} "
            f"notes_order_mismatch={tot.get('notes_order_mismatch')} "
            f"pua_chars={tot.get('pua_chars')} "
            f"price_unparsed={tot.get('price_unparsed')} "
            f"table_shape_warnings={tot.get('table_shape_warnings')} "
            f"field_x_order={tot.get('field_x_order')} "
            f"gate_page_errors={tot.get('gate_page_errors')} "
            f"pass={tot.get('pass')}",
            flush=True,
        )
        pu = report.get("price_unparsed") or []
        if pu:
            print("price_unparsed_list=" + json.dumps(pu, ensure_ascii=False), flush=True)
        tw = report.get("table_shape_warnings") or []
        if tw:
            print("table_shape_warnings_list=" + json.dumps(tw, ensure_ascii=False), flush=True)
        if not tot.get("pass"):
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
