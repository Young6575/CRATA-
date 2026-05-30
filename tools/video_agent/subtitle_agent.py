"""
subtitle_agent.py — 강의 영상 자동 자막 에이전트

사용법:
  python subtitle_agent.py prepare --source "D:/source/sample.mp4" --workspace-dir "D:/CRATA_Video_Work"
  python subtitle_agent.py status       # 현재 처리 상태
  python subtitle_agent.py transcribe --base-dir "D:/CRATA_Video_Work/_staging_..."   # 1단계: SRT 파일 생성
  python subtitle_agent.py review       # 2단계: 자막 품질 검토
  python subtitle_agent.py diarize      # 3단계: 화자분리 → ASS 파일 생성 (색상 구분)
  python subtitle_agent.py hardcode     # 4단계: 자막을 영상에 하드코딩
  python subtitle_agent.py preview      # 사진 오버레이 35초 테스트 클립 생성
  python subtitle_agent.py final        # 사진 오버레이 + 자막 전체 영상 인코딩

설정:
  HF_TOKEN   환경변수 또는 아래 HF_TOKEN 변수 (화자분리에 필요)
             HuggingFace 토큰 발급: https://hf.co/settings/tokens
             모델 동의 필요:
               https://hf.co/pyannote/speaker-diarization-3.1
               https://hf.co/pyannote/segmentation-3.0
               https://hf.co/pyannote/speaker-diarization-community-1

전사(transcribe):
  faster-whisper large-v3 모델을 GPU(CUDA)로 로컬 실행.
  첫 실행 시 모델 자동 다운로드 (~3GB).
  vad_filter=False 로 앞부분 누락 방지.
"""

import os
import re
import sys
import json
import time
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ── 기본 설정 ─────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_video_settings() -> dict:
    settings_path = PROJECT_ROOT / "처리관리" / "local_settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        video = settings.get("video") if isinstance(settings.get("video"), dict) else {}
        return video if isinstance(video, dict) else {}
    except Exception:
        return {}


def _default_base_dir() -> Path:
    env_path = os.environ.get("CRATA_VIDEO_BASE_DIR") or os.environ.get("CRATA_VIDEO_SOURCE_DIR")
    if env_path:
        return Path(env_path).expanduser()
    video_settings = _load_video_settings()
    source_dirs = video_settings.get("source_dirs")
    if isinstance(source_dirs, list) and source_dirs:
        return Path(str(source_dirs[0])).expanduser()
    return Path(r"H:\Q&A 강의 영상")


def default_workspace_root() -> Path:
    env_path = os.environ.get("CRATA_VIDEO_WORKSPACE_DIR")
    if env_path:
        return Path(env_path).expanduser()
    video_settings = _load_video_settings()
    configured = video_settings.get("workspace_dir") or video_settings.get("work_dir")
    if configured:
        return Path(str(configured)).expanduser()
    return BASE_DIR


BASE_DIR = _default_base_dir()
VIDEO_EXTENSIONS = {".mxf", ".mp4", ".mov", ".avi", ".mkv"}
SKIP_KEYWORDS = ("_vrew", "_sub", "_final", "자막포함", "preview_photo", "_proxy")
LANGUAGE      = "ko"
WHISPER_MODEL = "large-v3"   # faster-whisper 로컬 모델
WHISPER_DEVICE      = "cuda"     # GPU 없으면 "cpu"로 변경
WHISPER_COMPUTE     = "float16"  # CPU면 "int8"로 변경

HF_TOKEN = os.environ.get("HF_TOKEN", "")  # $env:HF_TOKEN = 'hf_...' 로 설정
WORKSPACE_MARKER = ".crata_video_workspace.json"

# ── 대시보드 상태 파일 경로 ────────────────────────────────────────────────────
STATUS_FILE: Path | None = Path(os.environ.get("CRATA_VIDEO_STATUS_FILE") or PROJECT_ROOT / "video_status.json")
LEGACY_STATUS_FILE: Path | None = Path(r"C:\Users\wnsdu\OneDrive\대시보드\video_status.json")
PROCESS_ID_BY_TASK = {
    "transcribe": "raw_transcribe",
    "diarize": "diarize",
    "review": "transcript_quality_review",
    "preview": "subtitle_preview_review",
    "hardcode": "burnin",
    "final": "final_encode",
}

# ── 화자 색상 설정 (ASS 형식: &HAABBGGRR) ────────────────────────────────────
SPEAKER_STYLES = {
    "SPEAKER_00": {"name": "화자1", "color_ass": "&H00FFFFFF", "color_hex": "#FFFFFF"},
    "SPEAKER_01": {"name": "화자2", "color_ass": "&H00FFFFFF", "color_hex": "#FFFFFF"},
    "SPEAKER_02": {"name": "화자3", "color_ass": "&H00FFFFFF", "color_hex": "#FFFFFF"},
}
DEFAULT_STYLE = {"name": "화자", "color_ass": "&H00FFFFFF", "color_hex": "#FFFFFF"}
DEFAULT_SUBTITLE_STYLE = {
    "font_name": "Malgun Gothic",
    "font_size": 54,
    "bold": True,
    "primary_color": "#FFFFFF",
    "outline_color": "#000000",
    "outline": 3,
    "shadow": 0,
    "margin_v": 74,
    "speaker_labels": False,
    "speaker_colors": False,
}
VISIBLE_SPEAKER_PREFIX = re.compile(r"^\s*(?:\[(?:강사|질문자|화자\s*\d+|SPEAKER[_\s-]*\d+)\]|(?:강사|질문자|화자\s*\d+|SPEAKER[_\s-]*\d+)\s*[:：])\s*", re.IGNORECASE)
NAMING_KEYWORDS = (
    "CRATA", "크라타", "문제해결방식검사", "문제해결", "문제 해결", "자기효능감검사", "자기효능감", "자기 효능감", "개인행동", "동기",
    "검사", "결과지", "역량", "중심역량", "핵심역량", "성장역량", "잠재역량", "유형", "행동유형",
    "조직", "인지", "행동", "강의", "수업", "설명", "질문", "답변",
)
NAMING_TOPIC_TERMS = (
    "문제해결방식검사", "개인행동동기검사", "자기효능감검사", "성장역량", "중심역량", "핵심역량", "잠재역량",
    "행동유형", "자기효능감", "문제해결", "개인행동", "동기", "결과지", "CRATA", "크라타",
)
NAMING_CONTEXT_TERMS = (
    ("질의응답", ("질문", "답변", "Q&A", "q&a", "큐앤에이")),
    ("강의", ("강의", "수업", "설명")),
)
NAMING_FILLERS = (
    "네", "예", "음", "어", "자", "그", "저", "이제", "그러면", "아", "일단", "뭐", "약간",
    "여러분", "안녕하세요", "시작하겠습니다", "시작하겠습니다.", "보겠습니다", "해볼게요",
)

# ── 사진 오버레이 설정 ─────────────────────────────────────────────────────────
# preview 명령으로 확인 후 값을 조정하세요

PHOTO_PATH = Path(r"C:\Users\wnsdu\Desktop\화면 캡처 2026-05-26 134336.png")
# TV 화면 위치 (3840x2160 기준): x=900~3100, y=120~830
# 두 사람 사이 갭: x≈1500~2300 → 문서를 그 안에 배치
PHOTO_WIDTH      = 490         # TV 화면 높이(710px)에 맞춘 A4 비율
PHOTO_X          = "1755"      # 두 사람 사이 + TV 화면 중앙
PHOTO_Y          = "118"       # TV 화면 상단에 맞춤
PHOTO_START      = 0           # 표시 시작 (초)
PHOTO_END        = 30          # 표시 종료 (초)
PHOTO_BRIGHTNESS = 0.0         # -1.0 ~ 1.0  (0 = 변화없음)
PHOTO_CONTRAST   = 1.0         # 0.0 ~ 2.0   (1 = 변화없음)
PHOTO_OPACITY    = 1.0         # 0.0 ~ 1.0   (1 = 완전불투명)

# ── 상태 파일 관리 ─────────────────────────────────────────────────────────────

_status_data: dict = {}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _clamp_number(value, default, minimum, maximum):
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return max(minimum, min(maximum, num))


def _normalize_hex(value: str, default: str = "#FFFFFF") -> str:
    raw = str(value or "").strip()
    return raw.upper() if re.match(r"^#[0-9A-Fa-f]{6}$", raw) else default


def hex_to_ass_color(value: str) -> str:
    color = _normalize_hex(value)
    rr = color[1:3]
    gg = color[3:5]
    bb = color[5:7]
    return f"&H00{bb}{gg}{rr}"


def load_subtitle_style() -> dict:
    style = dict(DEFAULT_SUBTITLE_STYLE)
    settings_path = PROJECT_ROOT / "처리관리" / "local_settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        video = settings.get("video") if isinstance(settings.get("video"), dict) else {}
        configured = video.get("subtitle_style") if isinstance(video.get("subtitle_style"), dict) else {}
    except Exception:
        configured = {}
    style.update({
        "font_name": str(configured.get("font_name") or configured.get("fontName") or style["font_name"]).strip() or style["font_name"],
        "font_size": int(_clamp_number(configured.get("font_size") or configured.get("fontSize"), style["font_size"], 28, 96)),
        "bold": bool(configured.get("bold", style["bold"])),
        "primary_color": _normalize_hex(configured.get("primary_color") or configured.get("primaryColor"), style["primary_color"]),
        "outline_color": _normalize_hex(configured.get("outline_color") or configured.get("outlineColor"), style["outline_color"]),
        "outline": _clamp_number(configured.get("outline"), style["outline"], 0, 8),
        "shadow": _clamp_number(configured.get("shadow"), style["shadow"], 0, 6),
        "margin_v": int(_clamp_number(configured.get("margin_v") or configured.get("marginV"), style["margin_v"], 20, 180)),
        "speaker_labels": False,
        "speaker_colors": False,
    })
    return style


def clean_visible_subtitle_text(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        cleaned = re.sub(r"</?font[^>]*>", "", line, flags=re.IGNORECASE)
        lines.append(VISIBLE_SPEAKER_PREFIX.sub("", cleaned).strip())
    return "\n".join(line for line in lines if line).strip()


def safe_folder_segment(value: str, fallback: str = "video") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = re.sub(r"_+", "_", text)
    if len(text) > 56:
        text = text[:56].rstrip(" .")
    return text or fallback


def unique_directory(parent: Path, name: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    base = safe_folder_segment(name)
    candidate = parent / base
    if not candidate.exists():
        return candidate
    for idx in range(2, 1000):
        candidate = parent / f"{base}_{idx:02d}"
        if not candidate.exists():
            return candidate
    return parent / f"{base}_{int(time.time())}"


def safe_video_filename(path: Path) -> str:
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", path.name).strip(" ._")
    return name or f"video{path.suffix.lower() or '.mp4'}"


def unique_file_path(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{idx:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}_{int(time.time())}{path.suffix}")


def collect_source_video_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.lower() in VIDEO_EXTENSIONS else []
    if not source.is_dir():
        return []
    files = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if any(part.startswith("$") or part.startswith(".") for part in path.parts):
            continue
        if any(kw in path.stem for kw in SKIP_KEYWORDS):
            continue
        files.append(path)
    return files


def workspace_marker_path(folder: Path) -> Path:
    return folder / WORKSPACE_MARKER


def write_workspace_marker(folder: Path, data: dict):
    marker = workspace_marker_path(folder)
    payload = {
        "created_at": _now_iso(),
        **data,
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_workspace_marker(folder: Path) -> dict:
    marker = workspace_marker_path(folder)
    if not marker.exists():
        return {}
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def prepare_workspace_from_source(source_raw: str, workspace_raw: str = "") -> Path:
    source = Path(source_raw).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"소스 경로가 없습니다: {source}")

    workspace_root = Path(workspace_raw).expanduser() if workspace_raw else default_workspace_root()
    workspace_root = workspace_root.resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)

    files = collect_source_video_files(source)
    if not files:
        raise RuntimeError("준비할 영상 파일이 없습니다.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    source_name = source.stem if source.is_file() else source.name

    if len(files) == 1:
        target_base = unique_directory(workspace_root, f"_staging_{stamp}_{safe_folder_segment(source_name)}")
        targets = [(files[0], target_base)]
        prepared_base = target_base
    else:
        batch_base = unique_directory(workspace_root, f"_batch_{stamp}_{safe_folder_segment(source_name)}")
        targets = []
        for src in files:
            folder = unique_directory(batch_base, f"_staging_{safe_folder_segment(src.stem)}")
            targets.append((src, folder))
        prepared_base = batch_base

    moved = []
    for src, folder in targets:
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / safe_video_filename(src)
        if src.resolve() != dest.resolve():
            shutil.move(str(src), str(dest))
        write_workspace_marker(folder, {
            "status": "staging",
            "original_path": str(src),
            "moved_from": str(src),
            "video_path": str(dest),
            "workspace_root": str(workspace_root),
            "naming_rule": "transcript_based_folder_and_file_names_after_raw_transcribe",
        })
        moved.append(str(dest))

    write_status({
        "status": "active",
        "progress": 0,
        "progress_pct": 0,
        "workspace_root": str(workspace_root),
        "workspace_path": str(prepared_base),
        "prepared_files": moved,
        "moved_files": moved,
        "original_source_path": str(source),
        "message": "영상 원본을 작업 폴더로 이동했습니다. 이후 명령은 이 경로를 --base-dir로 사용하세요.",
    })
    return prepared_base


def normalize_transcript_title_text(text: str) -> str:
    text = re.sub(r"\([^)]{1,20}\)", " ", str(text or ""))
    text = re.sub(r"\[[^\]]{1,20}\]", " ", text)
    text = re.sub(r"[`~!@#$%^&*+=|\\{}<>]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,;:!?，。")
    words = text.split()
    while words and words[0] in NAMING_FILLERS:
        words.pop(0)
    text = " ".join(words).strip(" .,;:!?，。")
    return text


def score_transcript_title_candidate(text: str, index: int) -> float:
    if not text:
        return -1000
    keyword_score = sum(1 for keyword in NAMING_KEYWORDS if keyword.lower() in text.lower()) * 14
    length = len(text)
    length_score = min(length, 54) * 0.7
    early_score = max(0, 36 - index)
    filler_penalty = sum(1 for filler in NAMING_FILLERS if text == filler or text.startswith(f"{filler} ")) * 12
    too_short_penalty = 30 if length < 8 else 0
    too_long_penalty = max(0, length - 70) * 0.4
    return keyword_score + length_score + early_score - filler_penalty - too_short_penalty - too_long_penalty


def is_sentence_like_title(text: str) -> bool:
    value = str(text or "").strip()
    if len(value) > 24:
        return True
    return bool(re.search(r"(습니다|합니다|됩니다|입니다|해요|할게요|보겠습니다|말씀드리겠습니다|대해서|통해서|그리고|그러면)", value))


def transcript_topic_title(segments: list[dict], fallback: str = "") -> str:
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    context_hits: dict[str, int] = {}

    for idx, seg in enumerate(segments[:120]):
        text = normalize_transcript_title_text(clean_visible_subtitle_text(seg.get("text", "")))
        if not text:
            continue
        lowered = text.lower()
        for term in NAMING_TOPIC_TERMS:
            if term.lower() not in lowered:
                continue
            first_seen.setdefault(term, idx)
            scores[term] = scores.get(term, 0) + 25 + max(0, 40 - idx) * 0.35 + min(len(term), 12) * 0.6
        for label, aliases in NAMING_CONTEXT_TERMS:
            if any(alias.lower() in lowered for alias in aliases):
                context_hits[label] = context_hits.get(label, 0) + 1

    if not scores:
        return ""

    ordered = sorted(
        scores,
        key=lambda term: (-scores[term], first_seen.get(term, 999), -len(term), term),
    )

    selected: list[str] = []
    for term in ordered:
        if any(term != existing and term in existing for existing in selected):
            continue
        if any(existing != term and existing in term for existing in selected):
            selected = [existing for existing in selected if existing not in term]
        candidate = "_".join([*selected, term])
        if len(candidate) <= 24:
            selected.append(term)
        if len(selected) >= 3:
            break

    if len(selected) == 1:
        for label, count in sorted(context_hits.items(), key=lambda item: -item[1]):
            candidate = "_".join([selected[0], label])
            if count >= 1 and len(candidate) <= 24:
                selected.append(label)
                break

    title = "_".join(selected).strip("_")
    if title and len(title) <= 24:
        return title
    return safe_folder_segment(fallback or title, fallback or "video")[:24].strip(" _")


def transcript_naming_from_srt(srt_path: Path, video: Path) -> dict:
    try:
        segments = read_srt(srt_path)
    except Exception:
        segments = []

    candidates = []
    keyword_hits = []
    for idx, seg in enumerate(segments[:48]):
        text = normalize_transcript_title_text(clean_visible_subtitle_text(seg.get("text", "")))
        if len(text) < 3:
            continue
        for keyword in NAMING_KEYWORDS:
            if keyword.lower() in text.lower() and keyword not in keyword_hits:
                keyword_hits.append(keyword)
        candidates.append((score_transcript_title_candidate(text, idx), idx, text))

    title = transcript_topic_title(segments, video.stem)

    if not title and candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        title = candidates[0][2]

    if not title or is_sentence_like_title(title):
        title = video.stem

    title = safe_folder_segment(title, video.stem)
    file_stem = safe_folder_segment(title, video.stem)
    stamp = datetime.now().strftime("%Y%m%d")
    folder_name = safe_folder_segment(f"{stamp}_{title}", f"{stamp}_{video.stem}")
    return {
        "title": title,
        "file_stem": file_stem,
        "folder_name": folder_name,
        "keywords": keyword_hits[:8],
        "candidate_count": len(candidates),
    }


def rename_workspace_files(folder: Path, old_stem: str, new_stem: str) -> dict[str, str]:
    if not old_stem or not new_stem or old_stem == new_stem:
        return {}

    renamed = {}
    candidates = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        if path.name == WORKSPACE_MARKER:
            continue
        if path.stem == old_stem or path.stem.startswith(f"{old_stem}_"):
            candidates.append(path)

    for path in sorted(candidates, key=lambda p: len(p.name), reverse=True):
        suffix_part = path.stem[len(old_stem):]
        target = path.with_name(f"{new_stem}{suffix_part}{path.suffix}")
        if target.exists() and target.resolve() != path.resolve():
            target = unique_file_path(target)
        if target.resolve() == path.resolve():
            continue
        path.rename(target)
        renamed[str(path)] = str(target)
    return renamed


def finalize_workspace_folder(video: Path, srt_path: Path) -> tuple[Path, Path]:
    folder = video.parent
    marker = read_workspace_marker(folder)
    staging = folder.name.startswith("_staging_") or bool(marker)
    if not staging:
        return video, srt_path
    existing_naming = marker.get("naming") if isinstance(marker.get("naming"), dict) else {}
    if marker.get("status") == "finalized" and existing_naming.get("file_stem") == video.stem:
        return video, srt_path

    naming = transcript_naming_from_srt(srt_path, video)
    target = folder if folder.name == naming["folder_name"] else unique_directory(folder.parent, naming["folder_name"])
    if target.resolve() == folder.resolve():
        target = folder
    else:
        try:
            folder.rename(target)
        except Exception as exc:
            print(f"  [경고] 작업 폴더 이름 변경 실패: {exc}")
            target = folder

    old_video = target / video.name if target != folder else video
    old_srt = target / srt_path.name if target != folder else srt_path
    renamed = rename_workspace_files(target, old_video.stem, naming["file_stem"])
    new_video = Path(renamed.get(str(old_video), str(old_video)))
    new_srt = Path(renamed.get(str(old_srt), str(old_srt)))
    marker_data = {
        **marker,
        "status": "finalized",
        "finalized_at": _now_iso(),
        "name_source": "raw_transcript_topic_analysis",
        "naming": naming,
        "folder_path": str(target),
        "video_path": str(new_video),
        "transcript_path": str(new_srt),
        "renamed_files": renamed,
    }
    write_workspace_marker(target, marker_data)
    write_status({
        "workspace_path": str(target),
        "source_path": str(target),
        "current_file": str(new_video),
        "renamed_files": renamed,
        "message": f"전사록 기준 이름 확정: {target.name} / {new_video.name}",
    })
    print(f"  → 전사록 기준 이름 확정: {target}")
    print(f"  → 영상 파일명: {new_video.name}")
    print(f"  → 전사 파일명: {new_srt.name}")
    return new_video, new_srt


def prepare_subtitle_for_display(subtitle: Path) -> Path:
    if subtitle.suffix.lower() != ".srt":
        return subtitle
    raw = subtitle.read_text(encoding="utf-8-sig", errors="replace")
    blocks = []
    changed = False
    for block in re.split(r"\n{2,}", raw.strip()):
        lines = block.splitlines()
        next_lines = []
        for line in lines:
            if re.match(r"^\d+$", line.strip()) or "-->" in line or not line.strip():
                next_lines.append(line)
                continue
            cleaned = clean_visible_subtitle_text(line)
            if cleaned != line:
                changed = True
            next_lines.append(cleaned)
        blocks.append("\n".join(next_lines))
    if not changed:
        return subtitle
    display_path = subtitle.with_name(subtitle.stem + "_display.srt")
    display_path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return display_path


def write_status(patch: dict | None = None):
    """STATUS_FILE에 현재 상태를 JSON으로 기록. patch가 있으면 병합."""
    if not STATUS_FILE:
        return
    try:
        if patch:
            _status_data.update(patch)
        _status_data["updated_at"] = _now_iso()
        targets = [STATUS_FILE]
        if LEGACY_STATUS_FILE and str(LEGACY_STATUS_FILE).lower() != str(STATUS_FILE).lower():
            targets.append(LEGACY_STATUS_FILE)
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(_status_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # 상태 기록 실패가 인코딩을 막으면 안 됨


def init_status(task: str, total: int):
    process_id = PROCESS_ID_BY_TASK.get(task, task)
    _status_data.clear()
    _status_data.update({
        "machine": "desktop",
        "status": "active",
        "task": task,
        "total": total,
        "total_files": total,
        "done": 0,
        "current_file": "",
        "current_process": process_id,
        "current_process_label": task,
        "progress": 0,
        "progress_pct": 0,
        "completed": [],
        "completed_files": [],
        "process_status": {
            process_id: {"status": "active", "progress": 0}
        },
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
    })
    write_status()


def finish_status():
    task = _status_data.get("task", "")
    process_id = PROCESS_ID_BY_TASK.get(task, task)
    process_status = _status_data.get("process_status") if isinstance(_status_data.get("process_status"), dict) else {}
    if process_id:
        process_status[process_id] = {"status": "done", "progress": 100}
    write_status({
        "current_file": "",
        "status": "done",
        "progress": 100,
        "progress_pct": 100,
        "done": _status_data.get("total", 0),
        "completed_files": _status_data.get("completed", []),
        "process_status": process_status,
        "finished_at": _now_iso(),
    })


def update_process_progress(process_id: str, pct: int | float, message: str = "", state: str = "active"):
    pct = max(0, min(100, int(pct)))
    process_status = _status_data.get("process_status") if isinstance(_status_data.get("process_status"), dict) else {}
    current = process_status.get(process_id) if isinstance(process_status.get(process_id), dict) else {}
    process_status[process_id] = {
        **current,
        "status": state,
        "progress": pct,
        "message": message or current.get("message", ""),
    }
    write_status({
        "status": "active" if state == "active" else _status_data.get("status", "active"),
        "current_process": process_id,
        "progress": pct,
        "progress_pct": pct,
        "process_status": process_status,
        "message": message,
    })


def add_process_result(process_id: str, result: dict):
    process_results = _status_data.get("process_results") if isinstance(_status_data.get("process_results"), dict) else {}
    items = process_results.get(process_id) if isinstance(process_results.get(process_id), list) else []
    target_path = str(result.get("path") or "").lower()
    if not any(str(item.get("path") or "").lower() == target_path for item in items if isinstance(item, dict)):
        items.append(result)
    process_results[process_id] = items
    write_status({"process_results": process_results})


# ─────────────────────────────────────────────────────────────────────────────


def get_ffmpeg() -> str:
    """시스템 ffmpeg 또는 imageio_ffmpeg 번들 경로 반환"""
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "imageio-ffmpeg", "--quiet"], check=True)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()


def get_ffprobe() -> str:
    """ffprobe 경로 반환 (ffmpeg와 같은 폴더에 있음)"""
    ff = get_ffmpeg()
    probe = Path(ff).parent / "ffprobe.exe"
    if probe.exists():
        return str(probe)
    p = shutil.which("ffprobe")
    return p or "ffprobe"


def get_duration(video_path: Path) -> float:
    """ffprobe로 영상 길이(초) 반환. 실패하면 0."""
    try:
        result = subprocess.run(
            [get_ffprobe(), "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(video_path)],
            capture_output=True, text=True, timeout=30
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _has_cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        pass
    try:
        result = subprocess.run(
            ["nvidia-smi"], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def get_whisper_model(device: str = WHISPER_DEVICE, compute: str = WHISPER_COMPUTE):
    """faster-whisper 모델 로드. 미설치 시 자동 설치."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper 패키지를 설치합니다...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "faster-whisper", "--quiet"],
            check=True,
        )
        from faster_whisper import WhisperModel

    print(f"  Whisper {WHISPER_MODEL} 모델 로드 중 (첫 실행 시 ~3GB 다운로드)...", flush=True)
    model = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute)
    print("  모델 로드 완료")
    return model


def get_openai_client():
    try:
        from openai import OpenAI
    except ImportError:
        print("openai 패키지를 설치합니다...")
        subprocess.run([sys.executable, "-m", "pip", "install", "openai", "--quiet"], check=True)
        from openai import OpenAI

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        print("\nOpenAI API 키가 없습니다.")
        print("  $env:OPENAI_API_KEY = 'sk-...'")
        sys.exit(1)
    return OpenAI(api_key=key)


def transcribe_to_srt_api(client, audio_path: Path) -> str:
    """OpenAI Whisper API로 전사 후 SRT 문자열 반환."""
    with open(audio_path, "rb") as f:
        return client.audio.transcriptions.create(
            model="whisper-1", file=f, language=LANGUAGE, response_format="srt",
        )


def collect_videos():
    videos = []
    for path in sorted(BASE_DIR.rglob("*")):
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if any(kw in path.stem for kw in SKIP_KEYWORDS):
            continue
        srt_path   = path.with_suffix(".srt")
        ass_path   = path.with_suffix(".ass")
        sub_path   = path.with_name(path.stem + "_sub.mp4")
        final_path = path.with_name(path.stem + "_final.mp4")
        videos.append({
            "video":     path,
            "srt":       srt_path,
            "ass":       ass_path,
            "sub":       sub_path,
            "final":     final_path,
            "has_srt":   srt_path.exists(),
            "has_ass":   ass_path.exists(),
            "has_sub":   sub_path.exists(),
            "has_final": final_path.exists(),
        })
    return videos


def extract_audio(video_path: Path, out_path: Path):
    cmd = [
        get_ffmpeg(), "-y", "-i", str(video_path),
        "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k",
        str(out_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 오디오 추출 실패:\n{result.stderr[-500:]}")


def transcribe_to_srt(model, audio_path: Path, total_sec: float = 0) -> str:
    """faster-whisper로 전사 후 SRT 문자열 반환."""
    segments, info = model.transcribe(
        str(audio_path),
        language=LANGUAGE,
        vad_filter=False,      # VAD OFF — 앞부분 누락 방지
        beam_size=5,
        word_timestamps=False,
    )
    if not total_sec:
        total_sec = float(getattr(info, "duration", 0) or get_duration(audio_path) or 0)

    lines = []
    last_pct = 0
    for i, seg in enumerate(segments, 1):
        start = format_srt_time(seg.start)
        end   = format_srt_time(seg.end)
        text  = seg.text.strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
        if total_sec:
            pct = min(99, max(10, int((float(seg.end) / total_sec) * 100)))
            if pct >= last_pct + 1:
                update_process_progress("raw_transcribe", pct, f"원본 전사 중 · {pct}%")
                last_pct = pct
    return "\n".join(lines)


# ── SRT 파싱/직렬화 ────────────────────────────────────────────────────────────

def parse_srt_time(t: str) -> float:
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def format_srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def format_ass_time(seconds: float) -> str:
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def parse_srt(srt_text: str) -> list[dict]:
    blocks = re.split(r"\n{2,}", srt_text.strip())
    segments = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            idx = int(lines[0].strip())
        except ValueError:
            continue
        m = re.match(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", lines[1])
        if not m:
            continue
        start = parse_srt_time(m.group(1))
        end   = parse_srt_time(m.group(2))
        text  = "\n".join(lines[2:]).strip()
        segments.append({"index": idx, "start": start, "end": end, "text": text})
    return segments


def read_srt(srt_path: Path) -> list[dict]:
    raw = srt_path.read_text(encoding="utf-8-sig", errors="replace")
    return parse_srt(raw)


# ── ffmpeg 진행률 추적 실행 ────────────────────────────────────────────────────

def run_ffmpeg_tracked(cmd: list[str], total_sec: float):
    """
    ffmpeg를 실행하면서 진행률을 STATUS_FILE에 실시간 기록.
    진행률 정보는 ffmpeg -progress 옵션으로 임시 파일에 수집.
    """
    ff = cmd[0]
    progress_tmp = Path(tempfile.mktemp(suffix="_ffprog.txt"))

    # -progress 옵션을 출력 파일 직전에 삽입
    tracked_cmd = cmd[:-1] + ["-progress", str(progress_tmp), cmd[-1]]

    proc = subprocess.Popen(
        tracked_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    last_pct = -1
    while proc.poll() is None:
        if STATUS_FILE and progress_tmp.exists():
            try:
                text = progress_tmp.read_text(encoding="utf-8", errors="ignore")
                # out_time_us가 마지막 완성된 값
                match = None
                for line in reversed(text.splitlines()):
                    if line.startswith("out_time_us="):
                        match = line
                        break
                if match and total_sec > 0:
                    us = int(match.split("=")[1])
                    pct = min(99, int(us / (total_sec * 1_000_000) * 100))
                    if pct != last_pct:
                        write_status({"progress_pct": pct})
                        last_pct = pct
            except Exception:
                pass
        time.sleep(2)

    progress_tmp.unlink(missing_ok=True)

    if proc.returncode not in (0, None):
        # 원래 명령으로 stderr 캡처해서 오류 메시지 얻기
        err = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg 실패:\n{err.stderr[-800:]}")


# ── review 명령어 ──────────────────────────────────────────────────────────────

REPEAT_PATTERN = re.compile(r"(.{3,})\1{1,}")
BROKEN_CHARS   = re.compile(r"�|·|­")  # 대체문자, 가운데점, 소프트하이픈
FOREIGN_SCRIPT = re.compile(r"[぀-ヿ㐀-䶿一-鿿Ѐ-ӿ]")  # 일본어, 한자, 러시아어

PREP_PHRASES = [
    "편집하고", "이렇게 붙여야", "어색해", "시작해볼까요", "카메라", "마이크",
    "잘 들려요", "테스트", "준비됐어요", "촬영", "시작할까요"
]


def review_srt(srt_path: Path) -> dict:
    issues = []
    try:
        segs = read_srt(srt_path)
    except Exception as e:
        return {"error": str(e), "issues": []}

    if not segs:
        return {"error": "자막 없음", "issues": []}

    total_duration = segs[-1]["end"] if segs else 0

    for seg in segs:
        duration = seg["end"] - seg["start"]
        text = seg["text"]

        if duration > 20:
            issues.append(f"  ⚠  #{seg['index']} [{format_srt_time(seg['start'])}] 자막 길이 {duration:.1f}초 (비정상)")

        if FOREIGN_SCRIPT.search(text):
            issues.append(f"  ✗  #{seg['index']} [{format_srt_time(seg['start'])}] 외국어 문자: {text[:40]!r}")
        elif BROKEN_CHARS.search(text):
            issues.append(f"  ✗  #{seg['index']} [{format_srt_time(seg['start'])}] 깨진 문자: {text[:40]!r}")

        if REPEAT_PATTERN.search(text):
            issues.append(f"  ⚠  #{seg['index']} [{format_srt_time(seg['start'])}] 반복 텍스트: {text[:40]!r}")

        if any(ph in text for ph in PREP_PHRASES) and seg["start"] < 120:
            issues.append(f"  ⚠  #{seg['index']} [{format_srt_time(seg['start'])}] 준비 대화 의심: {text[:40]!r}")

    return {"segments": len(segs), "duration": total_duration, "issues": issues}


def write_review_report(srt_path: Path, result: dict) -> Path:
    report_path = srt_path.with_name(srt_path.stem + "_review.md")
    issue_lines = result.get("issues") or []
    lines = [
        f"# 전사 품질검토: {srt_path.name}",
        "",
        f"- 자막 수: {result.get('segments', 0)}개",
        f"- 길이: {str(timedelta(seconds=int(result.get('duration', 0))))}",
        f"- 이슈: {len(issue_lines)}건",
        "",
        "## 검토 결과",
        "",
    ]
    if issue_lines:
        lines.extend(f"- {issue.strip()}" for issue in issue_lines)
    else:
        lines.append("- 품질검토 기준에서 발견된 자동 이슈가 없습니다.")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def cmd_review():
    videos = collect_videos()
    has_srt = [v for v in videos if v["has_srt"]]
    if not has_srt:
        print("검토할 SRT 파일이 없습니다.")
        return

    print(f"\n{'='*70}")
    print(f"  자막 품질 검토 ({len(has_srt)}개 파일)")
    print(f"{'='*70}\n")

    total_issues = 0
    init_status("review", len(has_srt))
    completed_names = []
    for i, item in enumerate(has_srt, 1):
        srt = item["srt"]
        label = f"{srt.parent.name}/{srt.name}"
        write_status({"done": i - 1, "current_file": label, "completed": completed_names})
        update_process_progress("transcript_quality_review", int((i - 1) / len(has_srt) * 100), f"전사 품질검토 중 · {label}")
        result = review_srt(srt)

        if "error" in result:
            print(f"[오류] {label}: {result['error']}")
            continue

        dur_str     = str(timedelta(seconds=int(result["duration"])))
        issue_count = len(result["issues"])
        total_issues += issue_count

        status = "✓ 정상" if issue_count == 0 else f"⚠ 이슈 {issue_count}건"
        print(f"  {label}")
        print(f"    자막 수: {result['segments']}개 | 길이: {dur_str} | {status}")
        for issue in result["issues"]:
            print(f"  {issue}")
        print()
        report = write_review_report(srt, result)
        completed_names.append(label)
        add_process_result("transcript_quality_review", {
            "title": "전사 품질검토 리포트",
            "path": str(report),
            "kind": "품질검토",
            "viewer": "review",
            "summary": status,
        })

    print(f"{'='*70}")
    print(f"  총 이슈: {total_issues}건")
    print(f"{'='*70}\n")
    update_process_progress("transcript_quality_review", 100, f"전사 품질검토 완료 · 총 이슈 {total_issues}건", state="done")
    write_status({"completed": completed_names, "completed_files": completed_names})


# ── diarize 명령어 ─────────────────────────────────────────────────────────────

def install_pyannote():
    print("pyannote.audio와 필수 패키지를 설치합니다...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install",
         "pyannote.audio", "torch", "soundfile", "--quiet"],
        check=True
    )


def run_diarization(audio_path: Path, token: str, num_speakers: int = 2):
    """
    pyannote/speaker-diarization-3.1 파이프라인으로 화자분리.
    필요 모델 동의 (HuggingFace 페이지에서 "Agree and access repository" 클릭):
      - https://hf.co/pyannote/speaker-diarization-3.1
      - https://hf.co/pyannote/segmentation-3.0
      - https://hf.co/pyannote/speaker-diarization-community-1  (PLDA 정확도 향상)
    """
    try:
        from pyannote.audio import Pipeline
        import torch
        import soundfile  # noqa: F401 — torchcodec 없이 WAV 읽기용
    except ImportError:
        install_pyannote()
        from pyannote.audio import Pipeline
        import torch

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"  (디바이스: {device})", flush=True)

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=token,
    )
    pipeline.to(device)

    _last = {"step": "", "pct": 0}

    def _hook(step, artifact=None, file=None, total=None, completed=None):
        if total and completed is not None:
            pct = int(completed / total * 100)
            if pct != _last["pct"] or step != _last["step"]:
                print(f"\r  [{step}] {pct}%  ", end="", flush=True)
                _last.update({"step": step, "pct": pct})

    result = pipeline(str(audio_path), hook=_hook, num_speakers=num_speakers)
    print()
    return [(t.start, t.end, sp) for t, _, sp in result.itertracks(yield_label=True)]


def assign_speakers(srt_segments: list[dict], diar_segments: list[tuple]) -> list[dict]:
    result = []
    for seg in srt_segments:
        speaker_time = defaultdict(float)
        for d_start, d_end, speaker in diar_segments:
            overlap = min(seg["end"], d_end) - max(seg["start"], d_start)
            if overlap > 0:
                speaker_time[speaker] += overlap
        dominant = max(speaker_time, key=speaker_time.get) if speaker_time else "SPEAKER_00"
        result.append({**seg, "speaker": dominant})
    return result


def normalize_speakers(segments_with_speakers: list[dict]) -> list[dict]:
    """발화 시간이 가장 많은 화자를 SPEAKER_00(강사)으로 정렬"""
    time_per_speaker = defaultdict(float)
    for seg in segments_with_speakers:
        time_per_speaker[seg["speaker"]] += seg["end"] - seg["start"]
    sorted_speakers = sorted(time_per_speaker, key=time_per_speaker.get, reverse=True)
    remap = {sp: f"SPEAKER_{i:02d}" for i, sp in enumerate(sorted_speakers)}
    return [{**seg, "speaker": remap[seg["speaker"]]} for seg in segments_with_speakers]


def get_speaker_style(speaker_id: str) -> dict:
    return SPEAKER_STYLES.get(speaker_id, DEFAULT_STYLE)


def speaker_review_label(speaker_id: str) -> str:
    style = get_speaker_style(speaker_id)
    name = str(style.get("name") or "").strip()
    if name:
        return name
    m = re.search(r"(\d+)$", str(speaker_id or ""))
    return f"화자{int(m.group(1)) + 1}" if m else "화자"


def create_ass_file(segments: list[dict], output_path: Path,
                    video_width=3840, video_height=2160):
    style = load_subtitle_style()
    style_name = "CRATA_Subtitle"
    primary = hex_to_ass_color(style["primary_color"])
    outline = hex_to_ass_color(style["outline_color"])
    bold = -1 if style["bold"] else 0
    font_size = int(style["font_size"])
    outline_width = style["outline"]
    shadow = style["shadow"]
    margin_v = int(style["margin_v"])
    styles_lines = [
        f"Style: {style_name},{style['font_name']},{font_size},{primary},"
        f"&H000000FF,{outline},&H80000000,{bold},0,0,0,"
        f"100,100,0,0,1,{outline_width},{shadow},2,10,10,{margin_v},1"
    ]

    header = (
        "[Script Info]\nTitle: 화자분리 자막\nScriptType: v4.00+\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {video_width}\nPlayResY: {video_height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles_lines)
        + "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = [header]
    for seg in segments:
        text = clean_visible_subtitle_text(seg["text"]).replace("\n", "\\N")
        speaker_name = speaker_review_label(seg.get("speaker", ""))
        lines.append(
            f"Dialogue: 0,{format_ass_time(seg['start'])},{format_ass_time(seg['end'])},"
            f"{style_name},{speaker_name},0,0,0,,{text}"
        )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def create_colored_srt(segments: list[dict], output_path: Path):
    lines = []
    for i, seg in enumerate(segments, 1):
        text = clean_visible_subtitle_text(seg["text"])
        speaker_name = speaker_review_label(seg.get("speaker", ""))
        review_text = f"[{speaker_name}] {text}" if speaker_name else text
        lines.append(
            f"{i}\n{format_srt_time(seg['start'])} --> {format_srt_time(seg['end'])}\n{review_text}\n"
        )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def cmd_diarize(yes: bool = False):
    token = HF_TOKEN or os.environ.get("HF_TOKEN", "")
    if not token:
        print("\n화자분리에는 HuggingFace 토큰이 필요합니다.")
        print("1. https://hf.co/settings/tokens 에서 Read 타입 토큰 발급")
        print("2. https://hf.co/pyannote/speaker-diarization-3.1 모델 이용 동의")
        print("3. https://hf.co/pyannote/segmentation-3.0 모델 이용 동의")
        print("4. https://hf.co/pyannote/speaker-diarization-community-1 모델 이용 동의 (PLDA 정확도 향상)")
        print("5. $env:HF_TOKEN = 'hf_...'")
        sys.exit(1)

    videos = collect_videos()
    ready  = [v for v in videos if v["has_srt"]]
    if not ready:
        print("SRT 파일이 있는 영상이 없습니다.")
        return

    print(f"\n화자분리 대상: {len(ready)}개\n")
    for item in ready:
        print(f"  - {item['video'].parent.name}/{item['video'].name}")

    if not yes and input("\n진행할까요? (y/n): ").strip().lower() != "y":
        print("취소됨.")
        return

    init_status("diarize", len(ready))
    completed_names = []
    for i, item in enumerate(ready, 1):
        video   = item["video"]
        srt     = item["srt"]
        ass_out = item["ass"]
        colored = srt.with_name(srt.stem + "_colored.srt")

        print(f"\n[{i}/{len(ready)}] {video.parent.name}/{video.name}")
        label = f"{video.parent.name}/{video.name}"
        write_status({"done": i - 1, "current_file": label, "completed": completed_names})

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_audio = Path(tmp.name)

        try:
            print("  → 오디오 추출 중...", end=" ", flush=True)
            update_process_progress("diarize", 5, "화자분리용 오디오 추출 중")
            extract_audio(video, tmp_audio)
            print("완료")

            print("  → 화자분리 중 (pyannote.audio)...", flush=True)
            update_process_progress("diarize", 20, "pyannote 화자분리 중")
            diar_segs = run_diarization(tmp_audio, token)
            print(f"  완료 ({len(diar_segs)}개 구간)")

            print("  → SRT 자막과 병합 중...", end=" ", flush=True)
            update_process_progress("diarize", 82, "화자 라벨과 전사 세그먼트 병합 중")
            srt_segs = read_srt(srt)
            tagged   = normalize_speakers(assign_speakers(srt_segs, diar_segs))

            counts  = defaultdict(int)
            for seg in tagged:
                counts[seg["speaker"]] += 1
            summary = ", ".join(
                f"{get_speaker_style(sp)['name']}: {cnt}구간"
                for sp, cnt in sorted(counts.items())
            )
            print(f"완료 ({summary})")

            create_ass_file(tagged, ass_out)
            print(f"  → ASS 저장: {ass_out.name}")
            create_colored_srt(tagged, colored)
            print(f"  → SRT 저장: {colored.name}")
            completed_names.append(label)
            add_process_result("diarize", {
                "title": "화자분리 ASS 자막",
                "path": str(ass_out),
                "kind": "화자분리",
                "viewer": "diarized",
                "note": summary,
            })
            add_process_result("diarize", {
                "title": "화자분리 색상 SRT",
                "path": str(colored),
                "kind": "화자분리",
                "viewer": "diarized",
                "note": summary,
            })

        except Exception as e:
            print(f"\n  [오류] {e}\n")
        finally:
            tmp_audio.unlink(missing_ok=True)

    print("\n3단계 완료. → 'python subtitle_agent.py final'")
    update_process_progress("diarize", 100, "화자분리 완료", state="done")
    write_status({"completed": completed_names, "completed_files": completed_names})


# ── photo overlay ──────────────────────────────────────────────────────────────

def _build_photo_filter(ass_path: Path) -> str:
    esc = str(ass_path).replace("\\", "/").replace(":", "\\:")
    return (
        f"[1:v]scale={PHOTO_WIDTH}:-1,"
        f"eq=brightness={PHOTO_BRIGHTNESS}:contrast={PHOTO_CONTRAST},"
        f"format=rgba,colorchannelmixer=aa={PHOTO_OPACITY}[img];"
        f"[0:v][img]overlay={PHOTO_X}:{PHOTO_Y}:"
        f"enable='between(t,{PHOTO_START},{PHOTO_END})'[vover];"
        f"[vover]ass='{esc}'[out]"
    )


def _hardcode_with_photo(video: Path, subtitle: Path, out_path: Path,
                          duration: int | None = None, fast: bool = False):
    total_sec = duration if duration else get_duration(video)
    ff     = get_ffmpeg()
    subtitle = prepare_subtitle_for_display(subtitle)
    fc     = _build_photo_filter(subtitle)
    crf    = "22" if fast else "18"
    preset = "fast" if fast else "slow"

    cmd = [ff, "-y", "-i", str(video), "-i", str(PHOTO_PATH)]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += [
        "-filter_complex", fc,
        "-map", "[out]", "-map", "0:a:0",
        "-c:v", "libx264", "-crf", crf, "-preset", preset,
        "-c:a", "aac", "-b:a", "192k",
        str(out_path),
    ]

    if STATUS_FILE and not fast:
        run_ffmpeg_tracked(cmd, total_sec)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 실패:\n{result.stderr[-800:]}")


def cmd_preview():
    if not PHOTO_PATH.exists():
        print(f"사진 파일 없음: {PHOTO_PATH}")
        return

    videos = collect_videos()
    ready  = [v for v in videos if v["has_ass"]] or [v for v in videos if v["has_srt"]]
    if not ready:
        print("자막 파일이 있는 영상이 없습니다.")
        return

    item     = ready[0]
    video    = item["video"]
    subtitle = item["ass"] if item["has_ass"] else item["srt"]
    out_path = video.with_name(video.stem + "_preview.mp4")

    print(f"\n미리보기 생성 중: {video.parent.name}/{video.name}")
    print(f"  사진:  {PHOTO_PATH.name}")
    print(f"  위치:  x={PHOTO_X}, y={PHOTO_Y}, 너비={PHOTO_WIDTH}px")
    print(f"  구간:  {PHOTO_START}s ~ {PHOTO_END}s")
    print(f"  출력:  {out_path}")
    print("  (35초 fast 인코딩...)", flush=True)

    init_status("preview", 1)
    write_status({
        "done": 0,
        "current_file": f"{video.parent.name}/{video.name}",
        "current_process": "subtitle_preview_review",
        "current_process_label": "미리보기 생성 중",
        "progress": 5,
        "progress_pct": 5,
        "completed": [],
        "completed_files": [],
    })
    update_process_progress("subtitle_preview_review", 15, "자막 미리보기 클립 생성 중")

    try:
        _hardcode_with_photo(video, subtitle, out_path, duration=35, fast=True)
        add_process_result("subtitle_preview_review", {
            "title": "자막 미리보기",
            "path": str(out_path),
            "kind": "미리보기",
            "viewer": "video",
        })
        process_status = _status_data.get("process_status") if isinstance(_status_data.get("process_status"), dict) else {}
        process_status["subtitle_preview_review"] = {
            "status": "waiting",
            "progress": 100,
            "message": "미리보기 파일 생성 완료. 확인 후 승인 대기 중입니다.",
        }
        write_status({
            "status": "waiting_preview_review",
            "current_process": "subtitle_preview_review",
            "current_process_label": "미리보기 확인 대기",
            "progress": 100,
            "progress_pct": 100,
            "preview_file": str(out_path),
            "message": "미리보기 파일 생성 완료. 확인 후 승인하면 최종 인코딩을 진행합니다.",
            "process_status": process_status,
        })
        print(f"\n완료: {out_path}")
        print("\nPHOTO_WIDTH / PHOTO_X / PHOTO_Y 조정 후 preview 재실행")
        print("만족하면: python subtitle_agent.py final")
    except Exception as e:
        process_status = _status_data.get("process_status") if isinstance(_status_data.get("process_status"), dict) else {}
        process_status["subtitle_preview_review"] = {
            "status": "error",
            "progress": int(_status_data.get("progress") or 0),
            "message": str(e),
        }
        write_status({
            "status": "error",
            "current_process": "subtitle_preview_review",
            "current_process_label": "미리보기 오류",
            "message": str(e),
            "process_status": process_status,
        })
        print(f"[오류] {e}")


def cmd_final(yes: bool = False):
    if not PHOTO_PATH.exists():
        print(f"사진 파일 없음: {PHOTO_PATH}")
        return

    videos = collect_videos()
    ready  = []
    for v in videos:
        if v["has_final"]:
            continue
        if v["has_ass"]:
            ready.append({**v, "use_sub": v["ass"], "sub_type": "ASS(화자분리)"})
        elif v["has_srt"]:
            ready.append({**v, "use_sub": v["srt"], "sub_type": "SRT(기본)"})

    if not ready:
        print("처리할 영상이 없습니다.")
        return

    print(f"\n최종 인코딩 대상: {len(ready)}개\n")
    for item in ready:
        print(f"  - {item['video'].parent.name}/{item['video'].name}  [{item['sub_type']}]")

    if not yes and input("\n진행할까요? (y/n): ").strip().lower() != "y":
        print("취소됨.")
        return

    init_status("final", len(ready))
    completed_names = []

    for i, item in enumerate(ready, 1):
        video    = item["video"]
        subtitle = item["use_sub"]
        out_path = video.with_name(video.stem + "_final.mp4")
        label    = f"{video.parent.name}/{video.name}"

        write_status({
            "done": i - 1,
            "current_file": label,
            "progress_pct": 0,
            "completed": completed_names,
        })

        print(f"\n[{i}/{len(ready)}] {label}")
        print(f"  자막: {subtitle.name}  [{item['sub_type']}]")
        print(f"  → 인코딩 중... (4K slow)", flush=True)

        try:
            _hardcode_with_photo(video, subtitle, out_path)
            completed_names.append(label)
            add_process_result("final_encode", {
                "title": "최종 인코딩 결과",
                "path": str(out_path),
                "kind": "최종 영상",
                "viewer": "video",
            })
            write_status({"progress_pct": 100, "completed": completed_names})
            print(f"  → 완료: {out_path.name}")
        except Exception as e:
            print(f"  [오류] {e}")

    finish_status()
    print("\n최종 인코딩 완료.")


# ── hardcode 명령어 ────────────────────────────────────────────────────────────

def hardcode_subtitles(video: Path, subtitle: Path) -> Path:
    """자막(SRT 또는 ASS)을 영상에 하드코딩 → *_sub.mp4 (사진 오버레이 없음)"""
    out = video.with_name(video.stem + "_sub.mp4")
    subtitle = prepare_subtitle_for_display(subtitle)
    esc = str(subtitle).replace("\\", "/").replace(":", "\\:")
    style = load_subtitle_style()

    if subtitle.suffix.lower() == ".ass":
        vf = f"ass='{esc}'"
    else:
        primary = hex_to_ass_color(style["primary_color"])
        outline = hex_to_ass_color(style["outline_color"])
        bold = -1 if style["bold"] else 0
        vf = (
            f"subtitles='{esc}':force_style='"
            f"FontName={style['font_name']},FontSize={int(style['font_size'])},"
            f"PrimaryColour={primary},OutlineColour={outline},"
            f"Bold={bold},Outline={style['outline']},Shadow={style['shadow']},MarginV={int(style['margin_v'])}'"
        )

    cmd = [
        get_ffmpeg(), "-y", "-i", str(video),
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "slow",
        "-c:a", "aac", "-b:a", "192k",
        str(out)
    ]

    total_sec = get_duration(video)
    if STATUS_FILE:
        run_ffmpeg_tracked(cmd, total_sec)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 실패:\n{result.stderr[-500:]}")
    return out


def cmd_hardcode(yes: bool = False):
    videos = collect_videos()
    ready  = []
    for v in videos:
        if v["has_ass"]:
            ready.append({**v, "use_sub": v["ass"], "sub_type": "ASS(화자분리)"})
        elif v["has_srt"]:
            ready.append({**v, "use_sub": v["srt"], "sub_type": "SRT(기본)"})

    if not ready:
        print("자막 파일이 있는 영상이 없습니다.")
        return

    print(f"\n하드코딩 대상: {len(ready)}개\n")
    for item in ready:
        print(f"  - {item['video'].parent.name}/{item['video'].name}  [{item['sub_type']}]")

    if not yes and input("\n진행할까요? (y/n): ").strip().lower() != "y":
        print("취소됨.")
        return

    init_status("hardcode", len(ready))
    completed_names = []

    for i, item in enumerate(ready, 1):
        video    = item["video"]
        subtitle = item["use_sub"]
        label    = f"{video.parent.name}/{video.name}"

        write_status({"done": i - 1, "current_file": label, "progress_pct": 0,
                      "completed": completed_names})
        print(f"\n[{i}/{len(ready)}] {label}")

        try:
            out = hardcode_subtitles(video, subtitle)
            completed_names.append(label)
            add_process_result("burnin", {
                "title": "자막 하드코딩 결과",
                "path": str(out),
                "kind": "자막 하드코딩",
                "viewer": "video",
            })
            write_status({"progress_pct": 100, "completed": completed_names})
            print(f"  → 완료: {out.name}")
        except Exception as e:
            print(f"  [오류] {e}")

    finish_status()
    print("\n4단계 완료.")


# ── transcribe 명령어 ──────────────────────────────────────────────────────────

def cmd_transcribe():
    videos  = collect_videos()
    pending = [v for v in videos if not v["has_srt"]]
    done    = [v for v in videos if v["has_srt"]]

    print(f"\n총 영상: {len(videos)}개")
    print(f"  이미 SRT 있음: {len(done)}개 (건너뜀)")
    print(f"  처리 대상: {len(pending)}개\n")

    if not pending:
        print("처리할 파일이 없습니다.")
        return

    # ── 전사 방식 결정 ──────────────────────────────────────────────────────
    use_api = False
    if not _has_cuda():
        print("\nGPU(CUDA)를 찾을 수 없습니다.")
        ans = input("OpenAI API로 전사할까요? (y=API / n=CPU로 로컬 실행): ").strip().lower()
        if ans == "y":
            use_api = True
            print("→ OpenAI API 방식으로 진행합니다.")
            transcriber = get_openai_client()
        else:
            print("→ CPU로 로컬 Whisper 실행합니다. (시간이 오래 걸릴 수 있습니다)")
            transcriber = get_whisper_model(device="cpu", compute="int8")
    else:
        print(f"GPU 확인됨. 로컬 Whisper {WHISPER_MODEL} (CUDA) 사용.")
        transcriber = get_whisper_model()
    # ────────────────────────────────────────────────────────────────────────

    init_status("transcribe", len(pending))
    completed_names = []

    for i, item in enumerate(pending, 1):
        video = item["video"]
        srt   = item["srt"]
        label = f"{video.parent.name}/{video.name}"

        write_status({"done": i - 1, "current_file": label, "completed": completed_names})
        print(f"[{i}/{len(pending)}] {label}")

        audio_suffix = ".mp3" if use_api else ".wav"
        with tempfile.NamedTemporaryFile(suffix=audio_suffix, delete=False) as tmp:
            tmp_audio = Path(tmp.name)

        try:
            print("  → 오디오 추출 중...", end=" ", flush=True)
            update_process_progress("raw_transcribe", 3, "오디오 추출 중")
            extract_audio(video, tmp_audio)
            update_process_progress("raw_transcribe", 8, "오디오 추출 완료")
            print("완료")

            if use_api:
                print("  → OpenAI Whisper API 전사 중...", end=" ", flush=True)
                update_process_progress("raw_transcribe", 10, "OpenAI Whisper API 전사 중")
                srt_content = transcribe_to_srt_api(transcriber, tmp_audio)
            else:
                print("  → Whisper large-v3 전사 중...", end=" ", flush=True)
                update_process_progress("raw_transcribe", 10, "Whisper large-v3 전사 중")
                srt_content = transcribe_to_srt(transcriber, tmp_audio, total_sec=get_duration(video))
            print("완료")

            srt.write_text(srt_content, encoding="utf-8")
            video, srt = finalize_workspace_folder(video, srt)
            label = f"{video.parent.name}/{video.name}"
            completed_names.append(label)
            add_process_result("raw_transcribe", {
                "title": "원본 전사록",
                "path": str(srt),
                "kind": "전사록",
                "viewer": "transcript",
            })
            update_process_progress("raw_transcribe", 100, "원본 전사 완료", state="done")
            write_status({"completed": completed_names, "completed_files": completed_names})
            print(f"  → SRT 저장: {srt.name}\n")

        except Exception as e:
            print(f"\n  [오류] {e}\n")
        finally:
            tmp_audio.unlink(missing_ok=True)

    finish_status()
    print("1단계 완료. → 'python subtitle_agent.py review'")


# ── status 명령어 ──────────────────────────────────────────────────────────────

def cmd_status():
    videos = collect_videos()
    print(f"\n{'파일':<52} {'SRT':^5} {'ASS':^5} {'_sub':^5} {'_final':^7}")
    print("-" * 78)
    for v in videos:
        name  = f"{v['video'].parent.name}/{v['video'].name}"
        flags = [
            "O" if v["has_srt"]   else "-",
            "O" if v["has_ass"]   else "-",
            "O" if v["has_sub"]   else "-",
            "O" if v["has_final"] else "-",
        ]
        print(f"{name:<52} {flags[0]:^5} {flags[1]:^5} {flags[2]:^5} {flags[3]:^7}")
    print()


def cmd_prepare(source_path: str = "", workspace_dir: str = ""):
    if not source_path:
        print("사용법: python subtitle_agent.py prepare --source <영상 파일 또는 폴더> [--workspace-dir <작업 루트>]")
        sys.exit(1)
    try:
        prepared = prepare_workspace_from_source(source_path, workspace_dir)
    except Exception as exc:
        print(f"[오류] 작업 폴더 준비 실패: {exc}")
        sys.exit(1)
    print(f"준비된 작업 경로: {prepared}")
    print("다음 명령부터 이 경로를 --base-dir 값으로 사용하세요.")


# ── 진입점 ─────────────────────────────────────────────────────────────────────

COMMANDS = {
    "prepare":    cmd_prepare,
    "transcribe": cmd_transcribe,
    "review":     cmd_review,
    "diarize":    cmd_diarize,
    "hardcode":   cmd_hardcode,
    "preview":    cmd_preview,
    "final":      cmd_final,
    "status":     cmd_status,
}

# 확인 프롬프트 없이 바로 실행하는 커맨드 목록
YES_SUPPORTED = {"diarize", "hardcode", "final"}


def pop_option(args: list[str], name: str) -> str:
    if name not in args:
        return ""
    idx = args.index(name)
    args.pop(idx)
    if idx >= len(args):
        return ""
    return args.pop(idx)


if __name__ == "__main__":
    args = sys.argv[1:]
    yes_flag = "--yes" in args or "-y" in args
    args = [a for a in args if a not in ("--yes", "-y")]
    source_arg = pop_option(args, "--source")
    workspace_arg = pop_option(args, "--workspace-dir")
    base_dir_arg = pop_option(args, "--base-dir")
    if base_dir_arg:
        BASE_DIR = Path(base_dir_arg).expanduser().resolve()

    cmd = args[0] if args else "status"
    if cmd not in COMMANDS:
        print(f"사용법: python subtitle_agent.py [{'|'.join(COMMANDS)}] [--base-dir <작업 폴더>] [--yes]")
        sys.exit(1)

    if cmd == "prepare":
        cmd_prepare(source_arg, workspace_arg)
    elif yes_flag and cmd in YES_SUPPORTED:
        COMMANDS[cmd](yes=True)
    else:
        COMMANDS[cmd]()
