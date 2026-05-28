---
name: export-review
description: CRATA 지식·결과지 문구 파일을 대표님 검수용 docx로 내보낸다. 블록마다 원본 + 수정사항·근거 입력칸을 만든다. Use when "검수 파일 만들어", "대표님 보실 수 있게 docx로", "검토용으로 빼줘", "export review" 등.
---

# export-review — 검수용 docx 생성

지식 파일이나 결과지 MASTER를 블록 단위 docx로 내보낸다. 대표님이 파일에서 바로 **수정사항·근거**를 입력하고, 그걸 그대로 `apply-review`로 반영한다.

## 워크플로

1. **대상·단위 정하기** — 어떤 파일인지, 블록을 어느 단위로 쪼갤지 정한다.
   - 결과지 MASTER → `--unit master` (연결키 단위, 원본 = text 블록 본문만)
   - 유형 카드형 지식(예: 조직10유형) → `--unit heading --level 3`
   - 방식 카드형(예: 행동방식4축) → `--unit heading --level 2`
   - 불릿 정의형(예: 16행동유형 유형별 정의) → `--unit bullet`
   - 모르면 `--unit auto` (연결키 있으면 master, 없으면 heading)
2. **생성**:
   ```
   python .claude/skills/export-review/scripts/export_review.py "<원본.md>" --unit <…> [--level N]
   ```
   - 출력: `검수/YYYY-MM-DD_<원본파일명>_검수.docx` (날짜로 버전 구분)
3. **확인** — 블록 수가 예상과 맞는지(콘솔 출력) 본다. 메타 섹션(예: "항목 틀")이 블록으로 잡혀도 무방 — 대표님이 비워두면 무시된다.
4. **전달 안내** — 대표님은 노란 칸(수정사항·근거)만 채우고 위치ID·원본은 그대로 둔다. 채운 파일을 그대로 주면 `apply-review`가 받는다.

## docx 구조 (블록마다 표)
| 항목 | 위치ID | 원본 | 수정사항(빈칸) | 근거(빈칸) |
- 위치ID = 반영 시 원본 위치를 찾는 앵커. **건드리면 안 됨.**

## 주의
- 이 스킬은 **읽기·생성 전용.** 원본 파일을 수정하지 않는다.
- 콘솔 출력이 cp949로 깨져 보여도 파일은 UTF-8 정상.
