#!/usr/bin/env node
/**
 * kordoc_convert — 파일 1개를 kordoc으로 Markdown + 이미지로 변환하고 결과 레코드(JSON)를 stdout에 낸다.
 * hwp2md.py(배치 오케스트레이터)가 파일마다 호출한다. 단독 실행도 가능.
 *
 *   node kordoc_convert.mjs <입력파일> <출력디렉토리>
 *
 * HWP2MD hwp_converter/run.mjs 에서 가져온 것: kordoc parse, 이미지 저장·링크 보정, 품질지표.
 * HWP2MD 복기 등록부의 결함을 여기서 고쳤다:
 *   D1 이미지 링크의 공백 → CommonMark `<...>` 표기로 감싸 링크가 깨지지 않게 한다.
 *   D2 링크 유효성 검사 추가 — 실제 파일 존재 여부를 센다.
 *   D3 HTML `<table>`(병합셀 표) 도 표 개수에 포함한다.
 * 출력 파일 이름은 입력 파일 이름(확장자 제외)을 그대로 쓴다. 입력 디렉토리에는 아무것도 쓰지 않는다.
 */
import { parse } from "kordoc"
import { mkdir, writeFile, access } from "fs/promises"
import path from "path"
import { createHash } from "crypto"

const HANGUL_RATIO_REVIEW_THRESHOLD = 0.05

function mdLink(target) {
  // 공백·괄호가 있으면 <...> 로 감싼다 (CommonMark 링크 목적지 규칙)
  return /[\s()]/.test(target) ? `<${target}>` : target
}

async function exists(p) { try { await access(p); return true } catch { return false } }

async function computeQuality(mdText, warningCount, outDir) {
  const visible = mdText.replace(/\s/g, "")
  const total = visible.length
  const hangul = (visible.match(/[가-힣]/g) ?? []).length
  const broken = (mdText.match(/�/g) ?? []).length
  const pipeRows = (mdText.match(/^\|.+\|\s*$/gm) ?? []).length
  const pipeTables = (mdText.match(/^\|[\s:|-]+\|\s*$/gm) ?? []).length   // 구분행 수 = 파이프 표 수
  const htmlTables = (mdText.match(/<table\b/gi) ?? []).length

  // 이미지 링크 유효성: ![..](target) 의 target 이 outDir 기준으로 실재하는가
  const linkRe = /!\[[^\]]*\]\((<[^>]+>|[^)\s]+)\)/g
  let linksTotal = 0, linksValid = 0
  for (const m of mdText.matchAll(linkRe)) {
    linksTotal++
    const target = m[1].startsWith("<") ? m[1].slice(1, -1) : m[1]
    if (await exists(path.join(outDir, target))) linksValid++
  }
  for (const m of mdText.matchAll(/<img\b[^>]*\bsrc="([^"]+)"/g)) {   // HTML 표 안의 그림(kordoc 4.x)
    linksTotal++
    if (await exists(path.join(outDir, m[1]))) linksValid++
  }

  const hangulRatio = total ? hangul / total : 0
  let flag = "ok"
  if (total === 0 || broken > 0) flag = "review"
  else if (hangulRatio < HANGUL_RATIO_REVIEW_THRESHOLD) flag = "review"
  else if (warningCount > 0) flag = "review"
  else if (linksTotal !== linksValid) flag = "review"

  return {
    total_chars: total,
    hangul_ratio: Math.round(hangulRatio * 10000) / 10000,
    broken_chars: broken,
    pipe_table_rows: pipeRows,
    pipe_tables: pipeTables,
    html_tables: htmlTables,
    image_links_total: linksTotal,
    image_links_valid: linksValid,
    parse_warnings: warningCount,
    quality_flag: flag,
  }
}

async function main() {
  const [src, outDir] = process.argv.slice(2)
  if (!src || !outDir) {
    console.error("usage: node kordoc_convert.mjs <input file> <output dir>")
    process.exit(2)
  }
  const started = performance.now()
  const baseName = path.parse(src).name
  const record = { file: src, engine: "kordoc", success: false, file_type: null,
                   output_markdown: null, images_dir: null, image_count: 0, warnings: [], error: null }
  try {
    const result = await parse(src)
    record.file_type = result.fileType ?? null
    if (!result.success) throw new Error(`${result.code ?? "PARSE_ERROR"}: ${result.error}`)

    await mkdir(outDir, { recursive: true })
    let markdown = result.markdown
    const images = result.images ?? []
    if (images.length > 0) {
      const imagesDirName = `${baseName}_images`
      await mkdir(path.join(outDir, imagesDirName), { recursive: true })
      for (const image of images) {
        const safe = path.basename(image.filename)
        await writeFile(path.join(outDir, imagesDirName, safe), image.data)
        markdown = markdown.replaceAll(`](${image.filename})`, `](${mdLink(`${imagesDirName}/${safe}`)})`)
        // kordoc 4.x 는 HTML 표 안의 그림을 <img src="파일"> 로 적는다 — 이것도 그림 폴더 경로로 고친다
        markdown = markdown.replaceAll(`src="${image.filename}"`, `src="${imagesDirName}/${safe}"`)
      }
      record.images_dir = imagesDirName
      record.image_count = images.length
    }
    const mdPath = path.join(outDir, `${baseName}.md`)
    await writeFile(mdPath, markdown, "utf-8")
    record.output_markdown = mdPath
    // 구조 그대로의 중간 파일(JSON): 병합 칸·표 안의 표·칸 안 그림을 Markdown 보다 정확히 담는다. 쓰기 스킬의 입력.
    // Markdown(보기용)과 달리 원본 구조를 지키는 옵션으로 한 번 더 읽는다(kordoc 4.x):
    //   layoutTables "keep" — 테두리 없는 틀 표도 표 그대로 · keepTrailingEmptyCols — 끝 빈 열(입력란) 보존 · keepEmptyParagraphs — 빈 줄 보존
    let structured = result
    try {
      const r2 = await parse(src, { layoutTables: "keep", keepTrailingEmptyCols: true, keepEmptyParagraphs: true })
      if (r2.success) structured = r2
    } catch { /* 옛 형식 등에서 옵션이 안 먹으면 보기용 결과로 대신한다 */ }
    // 두 번 읽으면 그림 번호(image_001…)가 서로 다르게 매겨질 수 있다(보기용 읽기는 일부 그림을 빼며 번호가 당겨진다).
    // 그래서 이름이 아니라 그림 내용(해시)으로 맞춘다: 같은 내용이면 이미 저장한 파일을, 새 내용이면 s_ 이름으로 따로 저장.
    const imgDir = `${baseName}_images`
    const byHash = new Map()        // 내용 해시 → 저장된 파일 이름
    for (const im of images) byHash.set(createHash("sha256").update(im.data).digest("hex"), path.basename(im.filename))
    const structName = new Map()    // 구조용 읽기의 그림 이름 → 저장된 파일 이름
    for (const im of structured.images ?? []) {
      const h = createHash("sha256").update(im.data).digest("hex")
      let name = byHash.get(h)
      if (!name) {
        name = "s_" + path.basename(im.filename)
        await mkdir(path.join(outDir, imgDir), { recursive: true })
        await writeFile(path.join(outDir, imgDir, name), im.data)
        byHash.set(h, name)
      }
      structName.set(path.basename(im.filename), name)
    }
    if (structured === result) for (const im of images) structName.set(path.basename(im.filename), path.basename(im.filename))
    if (byHash.size > 0) { record.images_dir = imgDir; record.image_count = byHash.size }
    // hwp 엔진은 칸의 밑줄·취소선을 칸 글(text)에만 붙이고 칸 문단(blocks)에는 붙이지 않는다 — 같은 줄을 찾아 옮겨 준다
    const bare = (s) => String(s ?? "").replace(/<\/?(?:u|s|sup|sub)>|~~|\s/g, "")
    const decoCellBlocks = (c, blocks) => {
      if (!/<u>|~~/.test(c.text ?? "")) return blocks
      const lines = String(c.text).split("\n")
      let i = 0
      for (const blk of blocks) {
        if (blk.type !== "paragraph" || !blk.text || /<u>|~~/.test(blk.text)) continue
        const want = bare(blk.text)
        for (let k = i; k < Math.min(lines.length, i + 50); k++) {
          if (bare(lines[k]) !== want) continue
          if (/<u>|~~/.test(lines[k])) blk.text = lines[k]
          i = k + 1
          break
        }
      }
      return blocks
    }
    const toJson = (b) => {
      const o = { type: b.type }
      for (const k of ["text", "level", "listType", "href", "footnoteText", "pageNumber"]) if (b[k] !== undefined) o[k] = b[k]
      // hwpx 의 밑줄·취소선은 글이 아니라 spans 에 담긴다 — hwp 쪽 표기(<u>…</u>, ~~…~~)와 같게 글에 되살린다
      if (b.spans?.some(s => s.underline || s.strike))
        o.text = b.spans.map(s => { let t = s.strike ? `~~${s.text}~~` : s.text; return s.underline ? `<u>${t}</u>` : t }).join("")
      if (b.style) o.style = b.style
      if (b.type === "image") {
        const name = structName.get(path.basename(String(b.text ?? b.imageData?.filename ?? "")))
        o.image = { path: name ? `${imgDir}/${name}` : null, mime: b.imageData?.mimeType ?? null }
        if (!o.image.path) o.image.missing = true
      }
      if (b.table) o.table = { rows: b.table.rows, cols: b.table.cols, hasHeader: b.table.hasHeader,
        ...(b.table.caption ? { caption: b.table.caption } : {}),
        cells: b.table.cells.map(row => row.map(c => ({ text: c.text, colSpan: c.colSpan, rowSpan: c.rowSpan,
          ...(c.isHeader ? { isHeader: true } : {}), ...(c.blocks ? { blocks: decoCellBlocks(c, c.blocks.map(toJson)) } : {}) }))) }
      if (b.children) o.children = b.children.map(toJson)
      return o
    }
    const doc = { format: "hangul-doc-json", version: 1,
                  note: "표 cells 는 병합으로 가려진 자리까지 채운 격자다. colSpan/rowSpan 은 병합 시작 칸에만 의미가 있다. 수식은 글 안의 $…$.",
                  source: { file: path.basename(src), fileType: result.fileType ?? null },
                  reader: { engine: "kordoc", options: structured === result ? "default" : "layoutTables=keep,keepTrailingEmptyCols,keepEmptyParagraphs" },
                  metadata: structured.metadata ?? null,
                  blocks: (structured.blocks ?? []).map(toJson) }
    const jsonPath = path.join(outDir, `${baseName}.json`)
    await writeFile(jsonPath, JSON.stringify(doc, null, 1), "utf-8")
    record.output_json = jsonPath
    record.warnings = (result.warnings ?? []).map(w => `${w.code}: ${w.message}`)
    record.metadata = result.metadata ?? null
    record.quality = await computeQuality(markdown, record.warnings.length, outDir)
    record.success = true
  } catch (err) {
    record.error = String(err?.message ?? err)
  }
  record.elapsed_seconds = Math.round((performance.now() - started) / 10) / 100
  process.stdout.write(JSON.stringify(record) + "\n")
  process.exit(record.success ? 0 : 1)
}

main().catch(err => { console.error(err); process.exit(1) })
