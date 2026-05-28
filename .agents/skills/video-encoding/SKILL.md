---
name: video-encoding
description: Use when processing CRATA desktop lecture videos: local faster-whisper large-v3 transcription, speaker diarization, transcript quality review, CRATA terminology correction, subtitle burn-in, preview, final encoding, MXF/MP4 handling, and dashboard video task execution.
---

# Video Encoding

CRATA 데스크탑 GPU 환경에서 강의 영상의 전사, 화자분리, 전사 품질검토, CRATA 용어 교정, 자막 하드코딩, 최종 인코딩을 처리한다.

## Core Rules

- 실제 영상 처리를 시뮬레이션하지 않는다.
- 결과 파일이 만들어지지 않았는데 작업을 완료 처리하지 않는다.
- 영상 원본, 결과 mp4, 모델 캐시, `__pycache__`는 Git에 올리지 않는다.
- 기본 전사는 로컬 `faster-whisper large-v3`를 사용한다.
- 기본 디바이스는 데스크탑 GPU `cuda`, compute는 `float16`, 언어는 `ko`다.
- 앞부분 누락을 막기 위해 전사 단계는 기본적으로 `vad_filter=False`를 사용한다.
- 화자분리 단계 전에는 대시보드에서 받은 화자 수를 확인한다.
- 전사 또는 화자분리를 수행했다면 전사 품질검토와 CRATA 용어 교정을 건너뛰지 않는다.

## Execution Targets

우선 프로젝트 내부 스크립트를 사용한다.

```text
tools/video_agent/subtitle_agent.py
```

아직 이관 전이면 기존 데스크탑 스크립트를 확인한다.

```text
C:\Users\wnsdu\Desktop\프로젝트\영상편집에이전트\subtitle_agent.py
H:\Q&A 강의 영상\subtitle_agent.py
```

## Dashboard Inputs

작업 요청에는 아래 값이 들어와야 한다.

- `sourcePath`: 데스크탑 기준 영상 파일 또는 폴더 경로
- `steps`: 실행 단계 목록
- `speakerCount`: 화자 수
- `preset`: 출력 품질/용도
- `runnerPreference`: `codex` 또는 `claude`

`diarize` 단계가 선택됐는데 `speakerCount`가 없으면 화자분리를 실행하지 말고 확인 요청을 남긴다.

## Required Workflow

1. 소스 경로가 데스크탑 서버에서 접근 가능한지 확인한다.
2. 폴더면 처리 가능한 영상 파일만 수집한다.
3. `status`로 SRT, ASS, `_sub`, `_final` 존재 여부를 확인한다.
4. 원본 전사: `large-v3`로 raw SRT/전사본을 만든다. 원본 전사는 보존한다.
5. 화자분리: 입력된 화자 수를 기준으로 raw 전사 세그먼트에 화자 라벨을 붙인다.
6. 전사 품질검토: 화자 라벨이 붙은 전사록 전체를 읽고 오인식, 문장 끊김, 반복, 어색한 문장을 검토한다.
7. CRATA 용어 교정: `지식/`, `결과지문구/`의 공식 용어를 기준으로 CRATA 관련 단어를 교정한다.
8. 화자분리 검토: 강사/질문자 라벨이 문맥상 뒤바뀐 구간, 짧은 맞장구, 질문 구간을 확인한다.
9. 검토 완료된 전사/ASS를 기준으로 자막 하드코딩과 최종 인코딩을 진행한다.
10. 완료 후 추가 편집이 필요한 결과물은 `처리관리/video_edit_queue.json`에 추가한다.

## Review Rules

- `raw.srt` 또는 원본 전사 파일은 덮어쓰지 않는다.
- 교정본은 `reviewed`가 드러나는 이름으로 별도 저장한다.
- 확신 없는 구간은 임의 수정하지 말고 `확인 필요`로 남긴다.
- CRATA 용어는 화면 표시 문구 기준으로 한국어를 우선한다.
- 의미가 바뀔 수 있는 교정은 Codex Output에 후보와 근거를 남긴다.
- 최종 인코딩은 검토 완료된 자막 기준으로만 진행한다.

## Status File

진행 중에는 `video_status.json`을 계속 갱신한다.

```json
{
  "status": "active",
  "progress": 0,
  "current_file": "",
  "batch_progress": 0,
  "total_files": 0,
  "completed_files": [],
  "current_step": 0,
  "message": "",
  "speaker_count": 2,
  "preset": "fast"
}
```

실패 시에는 `status`를 `error`로 두고, 어떤 파일에서 왜 실패했는지 `message`에 남긴다.

## Expected Outputs

- `.srt`: 원본 전사 자막
- `_reviewed.srt` 또는 `_reviewed.md`: 검토/용어 교정본
- `.ass`: 화자별 스타일 자막
- `_colored.srt`: 화자 색상 확인용 자막
- `_sub.mp4`: 자막 하드코딩 결과
- `_final.mp4`: 최종 렌더링 결과
- `_review.md`: 교정 근거, 확인 필요 구간, 화자분리 이슈

## Failure Handling

아래 경우에는 임의로 완료 처리하지 않는다.

- `HF_TOKEN`이 없어 화자분리를 못 하는 경우
- `ffmpeg`가 없거나 실행 실패한 경우
- CUDA 또는 faster-whisper 로딩 실패
- 소스 파일 경로가 노트북 기준 경로라 데스크탑에서 안 보이는 경우
- MXF 디코딩 실패
- 결과 파일이 생성되지 않은 경우
- 전사 품질검토 또는 CRATA 용어 교정을 수행하지 못한 경우

실패 원인과 필요한 조치를 Codex Output에 남긴다.
