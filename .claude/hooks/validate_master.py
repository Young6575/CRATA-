#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CRATA MASTER 문구 파일 검증기.

검사 항목:
  1. 연결키 중복 (파일 내 유일해야 함)
  2. 빈 ```text``` 블록
  3. 연결키 뒤에 text 블록이 없는 구조 깨짐

사용:
  - 수동:   python validate_master.py "경로/파일_MASTER.md" [...]
  - 인자 없으면 기본 MASTER 파일들을 검사
  - PostToolUse hook: stdin으로 받은 JSON의 file_path가 MASTER면 그 파일만 검사

종료 코드: 문제 있으면 2(훅에서 Claude에게 피드백), 없으면 0.
"""
import sys
import re
import json
import glob
from pathlib import Path

KEY_RE = re.compile(r"^연결키:\s*`([^`]+)`")
FENCE_OPEN_RE = re.compile(r"^```text\s*$")
FENCE_CLOSE_RE = re.compile(r"^```\s*$")


def find_default_masters():
    root = Path(__file__).resolve().parents[2]
    return [Path(p) for p in glob.glob(str(root / "**" / "*MASTER.md"), recursive=True)]


def validate(path: Path):
    problems = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return [f"파일을 읽을 수 없음: {e}"]

    keys = {}  # key -> [line numbers]
    i = 0
    n = len(lines)
    while i < n:
        m = KEY_RE.match(lines[i])
        if m:
            key = m.group(1)
            keys.setdefault(key, []).append(i + 1)
            # 이 연결키에 딸린 text 블록을 다음 연결키 전까지 탐색
            j = i + 1
            found_block = False
            block_empty = True
            while j < n and not KEY_RE.match(lines[j]):
                if FENCE_OPEN_RE.match(lines[j]):
                    found_block = True
                    j += 1
                    while j < n and not FENCE_CLOSE_RE.match(lines[j]):
                        if lines[j].strip():
                            block_empty = False
                        j += 1
                    break
                j += 1
            if not found_block:
                problems.append(f"L{i+1} `{key}`: text 블록이 없음 (구조 깨짐)")
            elif block_empty:
                problems.append(f"L{i+1} `{key}`: text 블록이 비어 있음")
        i += 1

    for key, locs in keys.items():
        if len(locs) > 1:
            problems.append(f"연결키 중복 `{key}`: 줄 {', '.join(map(str, locs))}")

    return problems


def targets_from_stdin():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return []
    fp = (data.get("tool_input") or {}).get("file_path", "")
    if fp and fp.endswith("MASTER.md"):
        return [Path(fp)]
    return []


def main():
    # Windows 콘솔/파이프 기본 인코딩이 cp949여도 한글 경로가 깨지지 않도록 UTF-8 강제
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if len(sys.argv) > 1:
        targets = [Path(p) for p in sys.argv[1:]]
    elif not sys.stdin.isatty():
        targets = targets_from_stdin()
    else:
        targets = find_default_masters()

    if not targets:
        return 0

    all_problems = []
    for path in targets:
        probs = validate(path)
        if probs:
            all_problems.append((path, probs))

    if all_problems:
        print("CRATA MASTER 검증 — 문제 발견:", file=sys.stderr)
        for path, probs in all_problems:
            print(f"\n[{path.name}]", file=sys.stderr)
            for p in probs:
                print(f"  - {p}", file=sys.stderr)
        return 2

    print("CRATA MASTER 검증 통과.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
