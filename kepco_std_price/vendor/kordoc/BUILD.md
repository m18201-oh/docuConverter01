# kordoc_bundle.mjs 만드는 법 (kordoc 4.19.0)

`kordoc_bundle.mjs` 는 `src/kordoc_convert.mjs` 와 kordoc 4.19.0(MIT) 및 의존 패키지를 esbuild 로 한 파일에 묶은 것이다. 쓰는 쪽은 `npm install` 없이 Node.js 20 이상만 있으면 된다.

## 쓰는 법

```bash
node kordoc_bundle.mjs <입력.hwp> <출력 폴더>
```

출력 폴더에 `<이름>.md`(보기용) · `<이름>.json`(구조용, `format: "hangul-doc-json"`) · `<이름>_images/` 가 생기고, 표준 출력 마지막에 결과 기록(JSON 한 덩이, `output_json` 등)이 찍힌다.
구조용 JSON 은 kordoc 을 `layoutTables: "keep"`, `keepTrailingEmptyCols: true`, `keepEmptyParagraphs: true` 로 읽은 블록 목록이다. 표 `cells` 는 병합으로 가려진 자리까지 채운 격자이고 병합 시작 칸에 `colSpan`·`rowSpan` 이 있다. 칸 안 문단·그림은 칸의 `blocks` 에 들어 있다. 위 첨자는 `<sup>…</sup>`, 수식은 `$…$`(LaTeX 꼴)로 적힌다.

## 다시 만들기 (`src/` 에서)

```bash
npm install kordoc@4.19.0 --omit=optional --ignore-scripts   # PDF·OCR 선택 부품은 받지 않는다
npm install esbuild@0.25 --no-save
npx esbuild kordoc_convert.mjs --bundle --platform=node --format=esm --target=node20 --minify \
  --outfile=../kordoc_bundle.mjs \
  "--banner:js=import{createRequire}from'module';const require=createRequire(import.meta.url);" \
  --alias:module=./module_shim.mjs --alias:pdfjs-dist=./pdfjs_stub \
  --external:@huggingface/transformers --external:@hyzyla/pdfium --external:onnxruntime-node \
  --external:sharp --external:puppeteer-core
```

- `--alias:pdfjs-dist=./pdfjs_stub`: kordoc 4.x 는 PDF 부품을 정적으로 불러온다. 한글 문서 읽기에는 쓰지 않으므로 `src/pdfjs_stub/` 의 빈 대역으로 바꾼다.
- `--alias:module=./module_shim.mjs`: kordoc 이 실행 중에 `createRequire(...)("cfb")` 로 부르는 패키지를 번들 안 사본으로 돌려준다.
- external 은 OCR·PDF·이미지·화면 그리기 선택 부품이다. 한글 문서 변환에는 쓰이지 않는다.
- 묶여 들어간 패키지와 라이선스 원문: `THIRD_PARTY_LICENSES.txt`.
