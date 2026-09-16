from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extract import extract_pdf
from .render_md import render_md


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
    args = ap.parse_args(argv)

    pages = _parse_pages(args.pages)
    result = extract_pdf(args.pdf, half=args.half, pages=pages)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out / "records.jsonl", result["records"])
    _write_jsonl(out / "groups.jsonl", result["groups"])
    _write_jsonl(out / "subheaders.jsonl", result["subheaders"])
    _write_jsonl(out / "pages.jsonl", result["pages"])
    render_md(result, out / "md", pdf_name=Path(args.pdf).name)
    return 0


if __name__ == "__main__":
    main()
