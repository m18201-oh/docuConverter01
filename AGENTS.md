# AGENTS.md — docuConverter01 (코딩 에이전트 공통 규칙, 저장소 루트)

> 팀 규칙 정본은 DevTeam 의 `AGENTS.md`(이 저장소 밖)다. 이 파일은 그 요약이며 저장소 안에서 일하는 모든 코딩 에이전트(헤르메스·Claude 서브에이전트·그 밖)가 읽는다. 헤르메스는 같은 폴더의 `.hermes.md` 를 먼저 읽으므로(1개만 로드) 두 파일의 요지는 같게 유지한다. 갱신: 2026-10-16.

## 이 저장소
GitHub `horu2day/docuConverter01` 을 `m18201-oh` 에 fork 한 것. PDF·HWP·EPUB → Markdown 변환 도구(Python)이며, `kepco_std_price/` 모듈이 한전 표준시장단가 책자를 JSON·md 로 바꾼다. 기본 브랜치 **`frontend`**. 환경 `uv sync`(Python 3.12) → `uv run python …`. 자체 시험 `uv run python -m kepco_std_price.selftest_md` · `selftest_hwp`. CI(`.github/workflows/selftests.yml`)가 PR 마다 ubuntu·windows 에서 돈다.

## 과제 시작
1. 코치가 준 과제서 `ref/TASK_<n>_<slug>.md` 가 이번 일의 전부(범위·브랜치·시험 명령·완료 조건). **과제서 없이 작업을 시작하지 않는다.**
2. `ref/HERMES_NOTES.md`(또는 코더 노트) — 이 저장소에서 전에 확인한 사실과 실수. 과제서와 다르면 과제서가 우선.
3. `git fetch origin` → `frontend` 최신에서 과제서가 지정한 브랜치.

## 경계
- 브랜치: 과제서가 지정한 `hermes/<n>-<slug>` · `agent/<n>-<slug>` 에서만. `frontend`·남의 브랜치 수정 금지.
- 범위: 과제서에 적힌 파일·모듈 밖은 고치지 않는다. 밖을 고쳐야 완료되면 멈추고 보고.
- 시험: 과제서의 시험 명령을 실제로 실행하고 출력 원문을 보고서에 붙인다. 시험·검사를 느슨하게 고쳐 통과시키지 않는다. 판정 도구(`ref/*/check_*.py`)는 고치지 않는다.
- 파괴 동작(삭제·초기화·덮어쓰기)·사례 값 하드코딩·낱말 목록 하드코딩 금지. 같은 항목을 세 번 고쳐도 남으면 멈춘다.
- 원격: 자기 브랜치만 푸시, `frontend` 대상 PR. `frontend` 직접 푸시·강제 푸시·병합·승인·원격 URL 변경 금지. 변경 요청은 같은 브랜치에 커밋 추가.
- 비밀: `.env`·키 파일을 열거나 만들거나 출력하지 않는다.
- `.hermes.md`·`ref/`·`log/`·`scripts/hermes_call.sh` 는 로컬 전용(커밋 안 됨). `.github/`·`AGENTS.md`·`selftest_*.py`·`conservation*.py` 는 CODEOWNERS 가 소유자 리뷰를 요구한다.

## 끝날 때
1. `ref/PR_<n>.md`(PR 본문: 과제·문제·원인·고친 내용·검증 출력 전문·남은 것) 2. `ref/REPORT_<n>.md`(바뀐 파일·시험 출력·커밋 해시·PR 주소·유보·5줄) 3. 새로 안 것은 노트에.
모르면 지어내지 말고 「유보」로 적는다. 병합 조건은 ruleset `frontend-protect`: PR · 승인 1(코치) · 상태 점검 `selftests` 2종 + `judge`(코치 판정).
