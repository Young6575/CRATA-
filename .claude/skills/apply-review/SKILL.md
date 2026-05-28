---
name: apply-review
description: 대표님이 채워서 돌려준 검수 docx를 읽어 지식·문구 파일에 반영한다. 수정사항이 있는 블록만 골라 제안→확인→반영하고 근거를 기록한다. Use when "검수한 파일 반영해", "이 docx 반영", "대표님이 수정한 거 적용", "apply review" 등.
---

# apply-review — 검수 docx 반영

`export-review`로 만든 docx를 대표님이 채워 돌려주면, 그 수정사항을 원본 파일에 반영한다.

## 워크플로

1. **파싱**:
   ```
   python .claude/skills/apply-review/scripts/parse_review.py "<채운.docx>"
   ```
   - 수정사항이 채워진 블록만 JSON으로 나온다: `{title, anchor, original, revision, reason}`.
   - `--all`을 붙이면 빈 블록까지 전부(점검용).
2. **원본 파일 찾기** — docx 제목/생성 메모의 "원본 파일" 경로, 또는 anchor로 어느 파일·어느 위치인지 확정한다.
   - master 단위면 anchor = 연결키 → 그 연결키의 ```text``` 본문만 고친다.
   - heading/bullet 단위면 anchor = 헤딩 경로/불릿 라벨 → 그 블록 본문을 고친다.
3. **정합성 점검** — 수정사항이 `지식/`의 다른 정의나 규칙과 충돌하면 멈추고 짚는다(CLAUDE.md 정합성 상시 감시).
4. **제안** — 블록별로 `original → revision`을 보여주고, 대표님 근거(reason)를 함께 제시. 아직 파일 안 고침.
5. **확인** — 사용자 OK 후에만 반영.
6. **반영 + 근거 기록**:
   - 원본 파일의 해당 위치를 수정(불변 규칙: 결과지면 text 블록·연결키 보존).
   - **블록마다** `decisions/log/`에 엔트리(`층`, 대상, 무엇을/왜=대표님 근거/파급).
   - 지식 수정이면 파급(연결된 문구·다른 정의)을 점검한다 — `revise-knowledge` 흐름 준용.

## 체크리스트
- [ ] 수정사항이 지식 정의·규칙과 모순 없는가 (있으면 멈추고 짚기)
- [ ] 결과지 문구면 text 블록 본문만 고쳤는가
- [ ] 대표님 근거를 그대로 `decisions/`에 남겼는가
- [ ] 파급(지식 수정 시)을 점검했는가

## 주의
- 대표님이 anchor(위치ID)·원본을 실수로 바꿔 매칭이 안 되면, 추측하지 말고 어느 블록인지 사용자에게 확인한다.
