#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PreToolUse hook: 지식/ 또는 *MASTER.md 편집 전 오늘 날짜 decisions/log 항목 확인.
항목이 없으면 경고 출력 + exit 2 (차단).

흐름: decisions/log/오늘_xxx.md 먼저 작성 → 그 후 지식·MASTER 편집 허용.
decisions/ 파일 자체는 항상 통과 (근거 작성을 막으면 안 됨).
"""
import sys
import json
from pathlib import Path
from datetime import date


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path", "")

    if not file_path:
        sys.exit(0)

    path = Path(file_path)

    # decisions/ 파일 자체는 통과 (근거 작성 허용)
    if "decisions" in path.parts:
        sys.exit(0)

    # rules/, .claude/ 등 내부 설정 파일도 통과
    skip_dirs = {"rules", ".claude", "memory", "검수", "기획안", "대상별_맥락"}
    if any(d in path.parts for d in skip_dirs):
        sys.exit(0)

    is_knowledge = "지식" in path.parts
    is_master = path.name.endswith("MASTER.md")

    if not (is_knowledge or is_master):
        sys.exit(0)

    # 오늘 날짜 decisions/log 항목 존재 여부 확인
    today = date.today().strftime("%Y-%m-%d")
    root = Path(__file__).resolve().parents[2]
    log_dir = root / "decisions" / "log"

    today_entries = list(log_dir.glob(f"{today}_*.md")) if log_dir.exists() else []

    if not today_entries:
        print(
            f"\n[차단] 오늘({today}) decisions/log 항목이 없습니다.\n"
            f"근거를 먼저 decisions/log/{today}_<설명>.md 로 기록한 뒤 편집하세요.\n"
            f"규칙: CLAUDE.md 불변규칙 2 — '근거 없는 수정 금지'",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
