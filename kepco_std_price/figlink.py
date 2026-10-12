"""5개 반기 뒤 — 2026H1 대표 그림 · 비교 그림 · 그림 색인 · md 그림 표식.

사용: python -m kepco_std_price.figlink <결과 뿌리> 2024H1 2024H2 2025H1 2025H2 2026H1
대표 반기는 인자 목록의 마지막이다.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image

from .figures import compact, fig_filename, group_code_set, match_groups_by_codes
from .render_md import render_md

CIRCLED = set("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳")
RE_CODE_LINE = re.compile(r"^[A-Z]{2}\d{3}\.\d{5}$")
IDX_KEYS = (
    "fig_id",
    "half",
    "group_id",
    "group_header",
    "k",
    "pdf_page",
    "printed_page",
    "page_half",
    "bbox",
    "lines",
    "n_images",
    "n_drawings",
    "file",
    "split_parts",
    "seq",
    "twin_group_id",
    "twin_fig_ids",
    "basis",
    "same",
    "used",
    "rep_files",
    "rep_half",
    "cmp_file",
    "review",
    "review_reason",
    "printed_page_ref",
)


def _is_note_like(c: str) -> bool:
    if not c:
        return False
    if c.startswith("※") or c[0] in CIRCLED:
        return True
    if c.endswith("다."):
        return True
    if RE_CODE_LINE.match(c):
        return True
    return False


def figure_text_bag(lines: list[str]) -> list[str]:
    """그룹의 그림 영역 글을 공백 제거·주석형 줄 제외 뒤 정렬한 목록."""
    bag = []
    for t in lines:
        c = compact(t)
        if not c or _is_note_like(c):
            continue
        bag.append(c)
    return sorted(bag)


def group_data_tuple(notes_items: list[str], rec_rows: list[tuple[str, str, str, str]]):
    notes = [compact(x) for x in notes_items]
    recs = tuple(
        (compact(a), compact(b), compact(c), compact(d)) for a, b, c, d in rec_rows
    )
    return (tuple(notes), recs)


def decide_usage(
    own_bag: list[str],
    twin_bag: list[str],
    own_data,
    twin_data,
    has_twin: bool,
    twin_has_figs: bool,
    *,
    is_rep_half: bool = False,
) -> dict:
    """그룹 둘의 그림 글자 묶음·공종 데이터를 받아 used·basis·same·review 를 돌려준다."""
    if is_rep_half:
        return {
            "used": "own",
            "basis": "figure_text" if (own_bag and twin_bag) else ("group_data" if has_twin else "no_twin"),
            "same": True if has_twin else None,
            "review": False,
            "review_reason": None,
        }
    if not has_twin or not twin_has_figs:
        return {
            "used": "own",
            "basis": "no_twin",
            "same": None,
            "review": True,
            "review_reason": "짝 그룹 없음",
        }
    if own_bag and twin_bag:
        same = list(own_bag) == list(twin_bag)
        if same:
            return {
                "used": "rep",
                "basis": "figure_text",
                "same": True,
                "review": False,
                "review_reason": None,
            }
        return {
            "used": "own",
            "basis": "figure_text",
            "same": False,
            "review": True,
            "review_reason": "그림 글자 다름",
        }
    same = own_data == twin_data
    if same:
        return {
            "used": "rep",
            "basis": "group_data",
            "same": True,
            "review": False,
            "review_reason": None,
        }
    return {
        "used": "own",
        "basis": "group_data",
        "same": False,
        "review": True,
        "review_reason": "공종 데이터 다름",
    }


def _jl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _write_jsonl(path: Path, rows: list[dict], keys: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps({k: row.get(k) for k in keys}, ensure_ascii=False) + "\n")


def _load_half(ed_dir: Path) -> dict:
    records = _jl(ed_dir / "records.jsonl")
    groups = _jl(ed_dir / "groups.jsonl")
    subheaders = _jl(ed_dir / "subheaders.jsonl")
    pages = _jl(ed_dir / "pages.jsonl")
    raw = _jl(ed_dir / "figures_raw.jsonl")
    half = ""
    if records:
        half = records[0].get("half") or ""
    elif groups:
        half = groups[0].get("half") or ""
    sha = ""
    pdf_name = ""
    md_dir = ed_dir / "md"
    if md_dir.exists():
        for p in sorted(md_dir.rglob("*.md")):
            text = p.read_text(encoding="utf-8")
            m = re.search(r"^sha256:\s*(\S+)", text, re.M)
            if m:
                sha = m.group(1)
            m = re.search(r"^source:\s*(.+)$", text, re.M)
            if m:
                pdf_name = m.group(1).strip()
            break
    return {
        "dir": ed_dir,
        "records": records,
        "groups": groups,
        "subheaders": subheaders,
        "pages": pages,
        "raw": raw,
        "half": half,
        "sha256": sha,
        "pdf_name": pdf_name,
    }


def _infer_pdf_path(md_dir: Path) -> str | Path | None:
    if not md_dir.exists():
        return None
    for p in sorted(md_dir.rglob("*.md")):
        text = p.read_text(encoding="utf-8")
        m = re.search(r"\]\(<([^>]+)#page=", text)
        if not m:
            m = re.search(r"\]\(<([^>]+)>\)", text)
        if m:
            rel = m.group(1).replace("%25", "%").replace("%23", "#").replace("%3F", "?")
            abs_path = (md_dir / "_field_" / rel).resolve()
            if abs_path.exists():
                return abs_path
        break
    return None


def _notes_items(g: dict) -> list[str]:
    return [(n.get("item") or "") for n in (g.get("notes") or [])]


def _rec_rows(gid: str | None, records: list[dict]) -> list[tuple[str, str, str, str]]:
    rows = []
    for r in records:
        if r.get("group_id") != gid:
            continue
        rows.append(
            (
                r.get("code") or "",
                r.get("name") or "",
                r.get("spec") or "",
                r.get("unit_norm") or r.get("unit") or "",
            )
        )
    return rows


def _half_order(h: str) -> int:
    return {"L": 0, "R": 1, "C": 2}.get(h or "", 9)


def _make_cmp(own_path: Path, twin_path: Path, out_path: Path) -> None:
    a = Image.open(own_path).convert("L")
    b = Image.open(twin_path).convert("L")
    w = max(a.width, b.width)
    h = max(a.height, b.height)

    def fit(im: Image.Image) -> Image.Image:
        canvas = Image.new("L", (w, h), 255)
        scale = min(w / im.width, h / im.height) if im.width and im.height else 1.0
        nw = max(1, int(round(im.width * scale)))
        nh = max(1, int(round(im.height * scale)))
        resized = im.resize((nw, nh), Image.Resampling.NEAREST)
        canvas.paste(resized, ((w - nw) // 2, (h - nh) // 2))
        return canvas

    fa, fb = fit(a), fit(b)
    overlay = Image.blend(fa, fb, 0.5)
    cmp = Image.new("L", (w * 3, h), 255)
    cmp.paste(fa, (0, 0))
    cmp.paste(fb, (w, 0))
    cmp.paste(overlay, (w * 2, 0))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmp.save(out_path, format="PNG")


def _copy_rep(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def link_figures(root: str | Path, halves: list[str]) -> None:
    root = Path(root)
    if not halves:
        raise SystemExit("오류: 반기 이름이 없습니다.")
    rep_half = halves[-1]
    ref_half = halves[-2] if len(halves) >= 2 else None
    data = {h: _load_half(root / h) for h in halves}
    rep = data[rep_half]
    rep_sets = [(g["group_id"], group_code_set(g["group_id"], rep["records"])) for g in rep["groups"]]
    rep_by_gid = {g["group_id"]: g for g in rep["groups"]}
    rep_figs: dict[str, list[dict]] = defaultdict(list)
    for x in rep["raw"]:
        if x.get("group_id"):
            rep_figs[x["group_id"]].append(x)
    for gid in rep_figs:
        rep_figs[gid].sort(key=lambda z: (z.get("k") or 0))

    ref_printed: dict[str, int | None] = {}
    if ref_half and ref_half in data:
        ref = data[ref_half]
        ref_sets = [(g["group_id"], group_code_set(g["group_id"], ref["records"])) for g in ref["groups"]]
        mapping_ref = match_groups_by_codes(rep_sets, ref_sets, min_jaccard=0.5)
        ref_g = {g["group_id"]: g for g in ref["groups"]}
        for rgid, tgid in mapping_ref.items():
            if tgid and tgid in ref_g:
                ref_printed[rgid] = ref_g[tgid].get("printed_page")

    for h in halves:
        ed = data[h]
        ed_dir: Path = ed["dir"]
        own_by_gid: dict[str | None, list[dict]] = defaultdict(list)
        for x in ed["raw"]:
            own_by_gid[x.get("group_id")].append(x)
        for gid in own_by_gid:
            own_by_gid[gid].sort(key=lambda z: (z.get("k") or 0))

        is_rep = h == rep_half
        mapping: dict[str, str | None] = {}
        if not is_rep:
            src_sets = [(g["group_id"], group_code_set(g["group_id"], ed["records"])) for g in ed["groups"]]
            mapping = match_groups_by_codes(src_sets, rep_sets, min_jaccard=0.5)

        gmap = {g["group_id"]: g for g in ed["groups"]}
        decisions: dict[str | None, dict] = {}
        for gid, figs in own_by_gid.items():
            if gid is None:
                decisions[gid] = {
                    "used": "own",
                    "basis": "no_twin",
                    "same": None,
                    "review": True,
                    "review_reason": "2026H1 쪽 공종 연결 실패" if is_rep else "짝 그룹 없음",
                    "twin_gid": None,
                    "piece_mismatch": False,
                }
                continue
            twin_gid = gid if is_rep else mapping.get(gid)
            twin_figs = rep_figs.get(twin_gid or "", [])
            has_twin = bool(twin_gid) if is_rep else (twin_gid is not None)
            own_lines = [ln for f in figs for ln in (f.get("lines") or [])]
            twin_lines = [ln for f in twin_figs for ln in (f.get("lines") or [])]
            own_bag = figure_text_bag(own_lines)
            twin_bag = figure_text_bag(twin_lines)
            og = gmap.get(gid) or {}
            tg = rep_by_gid.get(twin_gid) if twin_gid else None
            own_d = group_data_tuple(_notes_items(og), _rec_rows(gid, ed["records"]))
            twin_d = group_data_tuple(_notes_items(tg or {}), _rec_rows(twin_gid, rep["records"])) if tg else None
            d = decide_usage(
                own_bag,
                twin_bag,
                own_d,
                twin_d,
                has_twin=has_twin,
                twin_has_figs=bool(twin_figs),
                is_rep_half=is_rep,
            )
            piece_mismatch = bool(has_twin and twin_figs and len(figs) != len(twin_figs) and d["used"] == "rep")
            if piece_mismatch:
                d = dict(d)
                d["review"] = True
                d["review_reason"] = "조각 수 다름"
            d["twin_gid"] = twin_gid if has_twin else None
            d["piece_mismatch"] = piece_mismatch
            d["twin_figs"] = twin_figs
            decisions[gid] = d

        # 파일: 대표 PNG 복사 · 비교 그림
        seq_rows = sorted(
            ed["raw"],
            key=lambda z: (
                z.get("pdf_page") or 0,
                _half_order(z.get("page_half") or ""),
                (z.get("bbox") or [0, 0, 0, 0])[1],
                z.get("k") or 0,
            ),
        )
        index_rows: list[dict] = []
        md_figs: dict[str, list[dict]] = defaultdict(list)
        shown_for_group: dict[str, list[dict]] = {}

        for seq, x in enumerate(seq_rows, start=1):
            gid = x.get("group_id")
            d = decisions.get(gid) or {
                "used": "own",
                "basis": "no_twin",
                "same": None,
                "review": True,
                "review_reason": "짝 그룹 없음",
                "twin_gid": None,
                "piece_mismatch": False,
                "twin_figs": [],
            }
            twin_gid = d.get("twin_gid")
            twin_figs = d.get("twin_figs") or (rep_figs.get(twin_gid or "", []) if twin_gid else [])
            used = d["used"]
            piece_mismatch = d.get("piece_mismatch")
            own_figs = own_by_gid.get(gid) or []
            k = x.get("k") or 1

            twin_fig_ids: list[str] = []
            rep_files: list[str] = []
            cmp_file = None
            paired_twin = None
            if twin_figs and not is_rep:
                if used == "rep" and (not piece_mismatch) and len(own_figs) == len(twin_figs):
                    paired_twin = twin_figs[k - 1] if 0 <= k - 1 < len(twin_figs) else None
                    if paired_twin:
                        twin_fig_ids = [paired_twin["fig_id"]]
                        src = Path(rep["dir"]) / paired_twin["file"]
                        dest_rel = f"figures/rep/{Path(paired_twin['file']).name}"
                        _copy_rep(src, ed_dir / dest_rel)
                        rep_files = [dest_rel]
                elif used == "rep":
                    twin_fig_ids = [t["fig_id"] for t in twin_figs]
                    rep_files = []
                    for t in twin_figs:
                        src = Path(rep["dir"]) / t["file"]
                        dest_rel = f"figures/rep/{Path(t['file']).name}"
                        _copy_rep(src, ed_dir / dest_rel)
                        rep_files.append(dest_rel)
                else:
                    paired_twin = twin_figs[k - 1] if 0 <= k - 1 < len(twin_figs) else twin_figs[0]
                    twin_fig_ids = [paired_twin["fig_id"]] if paired_twin else [t["fig_id"] for t in twin_figs]

                twin_for_cmp = paired_twin or (twin_figs[k - 1] if 0 <= k - 1 < len(twin_figs) else None)
                if twin_for_cmp:
                    cmp_rel = f"figures/cmp/{fig_filename(x['fig_id']).replace('.png', '')}.png"
                    # fig_filename already ends with .png; use own file stem
                    cmp_rel = f"figures/cmp/{Path(x['file']).name}"
                    try:
                        _make_cmp(ed_dir / x["file"], Path(rep["dir"]) / twin_for_cmp["file"], ed_dir / cmp_rel)
                        cmp_file = cmp_rel
                    except OSError:
                        cmp_file = None

            row = dict(x)
            row["seq"] = seq
            row["twin_group_id"] = twin_gid
            row["twin_fig_ids"] = twin_fig_ids
            row["basis"] = d["basis"]
            row["same"] = d["same"]
            row["used"] = used
            row["rep_files"] = rep_files if used == "rep" else []
            row["rep_half"] = rep_half if used == "rep" else None
            row["cmp_file"] = cmp_file
            row["review"] = bool(d["review"])
            row["review_reason"] = d["review_reason"]
            if is_rep:
                row["printed_page_ref"] = ref_printed.get(gid)
            else:
                row["printed_page_ref"] = None
            index_rows.append(row)

            if gid:
                if used == "rep":
                    if gid not in shown_for_group:
                        if piece_mismatch or len(own_figs) != len(twin_figs):
                            shown = []
                            for i, t in enumerate(twin_figs, start=1):
                                dest_rel = f"figures/rep/{Path(t['file']).name}"
                                shown.append(
                                    {
                                        "k": i,
                                        "path": dest_rel,
                                        "source": f"출처: {rep_half} 대표 그림",
                                    }
                                )
                            shown_for_group[gid] = shown
                    # k-paired: one marker per own figure, using the matching rep
                    if not (piece_mismatch or (twin_figs and len(own_figs) != len(twin_figs))):
                        if paired_twin:
                            dest_rel = f"figures/rep/{Path(paired_twin['file']).name}"
                            md_figs[gid].append(
                                {
                                    "k": k,
                                    "path": dest_rel,
                                    "source": f"출처: {rep_half} 대표 그림",
                                }
                            )
                else:
                    md_figs[gid].append({"k": k, "path": x["file"], "source": None})
            else:
                md_figs["unassigned"].append({"k": k, "path": x["file"], "source": None})

        for gid, shown in shown_for_group.items():
            md_figs[gid] = shown

        _write_jsonl(ed_dir / "figures.jsonl", index_rows, IDX_KEYS)

        pdf_path = _infer_pdf_path(ed_dir / "md")
        result = {
            "records": ed["records"],
            "groups": ed["groups"],
            "subheaders": ed["subheaders"],
            "pages": ed["pages"],
            "half": ed["half"] or h,
            "sha256": ed["sha256"],
        }
        render_md(
            result,
            ed_dir / "md",
            pdf_name=ed["pdf_name"] or "",
            pdf_path=pdf_path,
            group_figures=dict(md_figs),
        )


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("사용: python -m kepco_std_price.figlink <결과 뿌리> 2024H1 2024H2 2025H1 2025H2 2026H1", file=sys.stderr)
        return 2
    link_figures(args[0], args[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
