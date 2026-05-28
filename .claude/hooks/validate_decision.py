#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PostToolUse hook: decisions/log/*.md 저장 후 필수 섹션·프론트매터 필드 검증.
누락이 있으면 경고 출력 + exit 2.

필수 프론트매터: 날짜, 층, 검사, 대상, 상태
필수 섹션: ## 무엇을 / ## 왜 / ## 파급
"""
import sys
import json
from pathlib import Path

REQUIRED_FRONTMATTER = ["날짜:", "층:", "검사:", "대상:", "상태:"]
REQUIRED_SECTIONS = ["## 무엇을", "## 왜", "## 파급"]


def validate(path: Path):
    problems = []
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return [f"파일을 읽을 수 없음: {e}"]

    for field in REQUIRED_FRONTMATTER:
        if field not in content:
            problems.append(f"프론트매터 필드 없음: `{field}`")

    for section in REQUIRED_SECTIONS:
        if section not in content:
            problems.append(f"필수 섹션 없음: `{section}`")

    return problems


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

    # decisions/log/*.md 만 대상
    parts = path.parts
    if "decisions" not in parts or "log" not in parts:
        sys.exit(0)
    if not path.name.endswith(".md"):
        sys.exit(0)

    problems = validate(path)

    if problems:
        print(
            f"\n[decisions 검증 실패] {path.name}",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\n필수 항목을 채운 뒤 다시 저장하세요.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"decisions 검증 통과: {path.name}")
    sys.exit(0)


if __name__ == "__main__":
    main()
