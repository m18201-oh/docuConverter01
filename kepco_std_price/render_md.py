"""레코드 → 대분류별 Markdown."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path


def _esc(s: str | None) -> str:
    if s is None:
        return ""
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_md(result: dict, md_dir: str | Path, pdf_name: str) -> None:
    md_dir = Path(md_dir)
    md_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = result.get("records") or []
    groups: list[dict] = result.get("groups") or []
    half = result.get("half") or ""
    sha = result.get("sha256") or ""
    pages = result.get("pages") or []
    layout = pages[0]["layout"] if pages else ""
    page_nums = [p["pdf_page"] for p in pages]
    page_range = f"{min(page_nums)}-{max(page_nums)}" if page_nums else ""

    gmap = {g["group_id"]: g for g in groups}
    by_major: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_major[r.get("major") or r["code"][0]].append(r)

    # preserve group order
    group_order = [g["group_id"] for g in groups]

    for major, recs in by_major.items():
        major_name = ""
        for r in recs:
            g = gmap.get(r.get("group_id"))
            if g and g.get("major_name"):
                major_name = g["major_name"]
                break
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", major_name)
        fname = f"{major}_{safe_name}.md" if safe_name else f"{major}_.md"
        # records grouped
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
            lines.append(f"## ■ {header}   (대분류 {gmajor} {gname}){extra}")
            lines.append("")
            lines.append("| 공종코드 | 공종명칭 | 규격 | 단위 | 단가 | 노무비율 | 원문 |")
            lines.append("|---|---|---|---|---|---|---|")
            for r in buckets[gid]:
                if r.get("status") == "abolished":
                    price_cell = "폐지"
                    labor_cell = r.get("abolished_at") or r.get("labor_raw") or ""
                else:
                    price_cell = r.get("price_raw") or ""
                    labor_cell = r.get("labor_raw") or ""
                link = f"[p.{r['pdf_page']}]({pdf_name}#page={r['pdf_page']})"
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            _esc(r.get("code")),
                            _esc(r.get("name")),
                            _esc(r.get("spec")),
                            _esc(r.get("unit")),
                            _esc(price_cell),
                            _esc(labor_cell),
                            link,
                        ]
                    )
                    + " |"
                )
            notes = g.get("notes") or []
            if notes:
                lines.append("")
                lines.append("**【단가정의】**")
                for n in notes:
                    lines.append(n.get("item") or "")
            lines.append("")

        (md_dir / fname).write_text("\n".join(lines), encoding="utf-8")
