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
- 작업 요청을 받으면 먼저 새 작업 폴더를 만들고 요청 영상 파일 원본을 그 폴더 안으로 이동한다. 대용량 원본 영상이 원래 위치와 작업 폴더에 중복으로 남으면 안 된다.
- 작업 폴더명과 영상 파일명, 전사·자막·검토 파일명은 전사 완료 후 전사록 내용을 분석한 공통 이름으로 확정한다. 전사 전에는 `_staging_YYYYMMDD_HHMMSS_원본명` 같은 임시 이름을 쓴다.
- 전사록, 검토 리포트, ASS/SRT, 미리보기, 하드코딩 결과, 최종 인코딩 파일은 모두 해당 작업 폴더 안에 저장한다. 원본 소스 폴더에는 영상 사본이나 산출물을 남기지 않는다.
- 기본 전사는 로컬 `faster-whisper large-v3`를 사용한다.
- 기본 디바이스는 데스크탑 GPU `cuda`, compute는 `float16`, 언어는 `ko`다.
- 앞부분 누락을 막기 위해 전사 단계는 기본적으로 `vad_filter=False`를 사용한다.
- 화자분리 단계 전에는 대시보드에서 받은 화자 수를 확인한다.
- 전사 또는 화자분리를 수행했다면 전사 품질검토와 CRATA 용어 교정을 건너뛰지 않는다.
- 자막 하드코딩 또는 최종 인코딩 전에는 반드시 짧은 미리보기 클립을 만들고 확인을 받아야 한다.
- 미리보기 확인 전에는 전체 길이 최종 인코딩을 진행하지 않는다.
- 화자분리 검토까지 반영한 파일이 최종 전사록이다. 이 파일을 기준으로 사용자가 대시보드에서 크게 읽고 채팅으로 추가 수정을 요청한다.
- 최종 전사록 검토 화면에는 화자 구분이 보여야 한다. 검토용 SRT에는 `[화자1]`, `[강사]`, `[질문자]` 같은 접두어를 보존하거나 ASS `Name` 필드에 화자명을 남긴다.
- 최종 표시 자막에는 `[강사]`, `[질문자]` 같은 화자 접두어를 넣지 않는다.
- 화자별 색상은 사용하지 않고 흰색 자막으로 통일한다. 글자 크기, 굵기, 외곽선, 하단 여백은 프로젝트의 고정 기본값을 따른다.
- 모델 다운로드 중에는 다운로드 대상과 진행률을 상태 파일에 남긴다.
- 각 프로세스에서 만들어진 파일은 해당 프로세스 결과로 기록한다.

## Execution Targets

우선 프로젝트 내부 스크립트를 사용한다.

```text
tools/video_agent/subtitle_agent.py
```

이 내부 스크립트는 `faster-whisper` 세그먼트가 생성될 때마다 `raw_transcribe` 진행률을 갱신한다. 전사 진행률이 0%에 머물면 외부 legacy 스크립트를 직접 실행하고 있는지 먼저 확인한다.

아직 이관 전이면 기존 데스크탑 스크립트를 확인한다.

```text
C:\Users\wnsdu\Desktop\프로젝트\영상편집에이전트\subtitle_agent.py
H:\Q&A 강의 영상\subtitle_agent.py
```

## Dashboard Inputs

작업 요청에는 아래 값이 들어와야 한다.

- `sourcePath`: 데스크탑 기준 영상 파일 또는 폴더 경로
- `videoWorkspaceRoot` 또는 `처리관리/local_settings.json`의 `video.workspace_dir`: 작업 폴더를 만들 루트
- `steps`: 실행 단계 목록
- `speakerCount`: 화자 수
- `preset`: 출력 품질/용도
- `runnerPreference`: `codex` 또는 `claude`
- `subtitleStyle`: 대시보드에서 저장한 자막 표시 스타일

`diarize` 단계가 선택됐는데 `speakerCount`가 없으면 화자분리를 실행하지 말고 확인 요청을 남긴다.
`burnin` 또는 `encode` 단계가 선택됐으면 `preview` 검수 단계를 필수로 포함한다.

## Required Workflow

1. 소스 경로가 데스크탑 서버에서 접근 가능한지 확인한다.
2. 내부 스크립트의 prepare 명령으로 새 작업 폴더를 만들고 요청 영상 파일 원본을 이동한다.
   ```text
   python tools/video_agent/subtitle_agent.py prepare --source "<sourcePath>" --workspace-dir "<videoWorkspaceRoot>"
   ```
3. prepare가 출력한 `준비된 작업 경로`를 이후 모든 명령의 `--base-dir`로 사용한다.
4. 폴더면 처리 가능한 영상 파일만 수집한다.
5. `status`로 SRT, ASS, `_sub`, `_final` 존재 여부를 확인한다.
6. 원본 전사: `large-v3`로 raw SRT/전사본을 만든다. 원본 전사는 보존한다.
7. 전사 완료 직후 내부 스크립트가 전사록의 유의미한 문장과 핵심어를 기준으로 작업 폴더명, 영상 파일명, 원본 전사 파일명을 공통 stem으로 확정한다. 이후 단계는 `video_status.json.workspace_path`의 새 경로를 `--base-dir`로 사용한다.
8. 화자분리: 입력된 화자 수를 기준으로 raw 전사 세그먼트에 화자 라벨을 붙인다.
9. 전사 품질검토: 화자 라벨이 붙은 전사록 전체를 읽고 오인식, 문장 끊김, 반복, 어색한 문장을 검토한다.
10. CRATA 용어 교정: `지식/`, `결과지문구/`의 공식 용어를 기준으로 CRATA 관련 단어를 교정한다.
11. 화자분리 검토: 강사/질문자 라벨이 문맥상 뒤바뀐 구간, 짧은 맞장구, 질문 구간을 확인한다.
12. 최종 전사록 저장: 화자분리 검토까지 반영한 전사록을 `_final_reviewed.srt` 또는 `_speaker_reviewed.srt`로 저장하고 `process_results.speaker_review`에 `viewer: "transcript"`로 기록한다. 검토용 전사록에는 화자 구분을 남기되, 최종 하드코딩용 표시 텍스트에서는 화자 접두어를 제거한다.
13. 사용자 전사록 검토: 대시보드의 최종 전사록 검토 패널에서 사용자가 수정 요청을 보낼 수 있으므로, 추가 요청이 들어오면 같은 최종 전사록을 다시 읽고 별도 검토본으로 갱신한다.
14. 자막 미리보기: 최종 전사록/ASS로 30~60초 미리보기 클립을 작업 폴더 안에 생성한다.
15. 미리보기 검수: 자막 크기, 위치, 하단 여백, 줄 수, 화자 색상, 얼굴/자료 화면 가림 여부를 확인할 수 있게 파일 경로를 남긴다.
16. 승인 대기: `video_status.json`을 `waiting_preview_review`로 갱신하고, 사용자가 확인하기 전에는 최종 인코딩을 멈춘다.
17. 승인 후 최종 처리: 사용자가 미리보기를 승인한 경우에만 전체 자막 하드코딩과 최종 인코딩을 진행한다.
18. 완료 후 추가 편집이 필요한 결과물은 `처리관리/video_edit_queue.json`에 추가한다.

## Review Rules

- `raw.srt` 또는 원본 전사 파일은 덮어쓰지 않는다.
- 교정본은 `reviewed`가 드러나는 이름으로 별도 저장한다.
- 최종 전사록은 화자분리 검토까지 반영된 `_final_reviewed.srt` 또는 `_speaker_reviewed.srt`이며, 최종 인코딩 입력도 이 파일을 기준으로 한다.
- 검토 화면용 전사록에는 화자 정보가 있어야 한다. 실제 자막 출력에는 보이지 않아야 하므로 하드코딩 전 `prepare_subtitle_for_display` 또는 동등한 정리 단계를 거친다.
- 확신 없는 구간은 임의 수정하지 말고 `확인 필요`로 남긴다.
- CRATA 용어는 화면 표시 문구 기준으로 한국어를 우선한다.
- 의미가 바뀔 수 있는 교정은 Codex Output에 후보와 근거를 남긴다.
- 최종 인코딩은 검토 완료된 자막과 승인된 미리보기 기준으로만 진행한다.

## Preview Gate

- 미리보기는 전체 인코딩 전에 만드는 짧은 검수용 결과물이다.
- 기본 길이는 30~60초이며, 가능하면 자막이 많이 나오는 구간과 CRATA 용어가 등장하는 구간을 포함한다.
- 확인 항목: 글자 크기, 화면 내 위치, 하단 여백, 2줄 이상 표시, 화자별 색상, 얼굴/자료 화면 가림, 한글 가독성.
- 화자별 색상은 기본적으로 쓰지 않는다. 검수 항목에서는 흰색 통일 자막의 크기, 굵기, 외곽선, 하단 여백을 확인한다.
- 미리보기 파일 이름에는 `preview`가 드러나야 한다.
- 미리보기 생성 후 `video_status.json`에 `status: "waiting_preview_review"`와 `preview_file`을 기록한다.
- 사용자가 승인하지 않았으면 `_final.mp4` 생성으로 넘어가지 않는다.

## Status File

진행 중에는 `video_status.json`을 계속 갱신한다.
프로젝트 루트의 `video_status.json`이 우선이며, 기존 데스크탑 영상 에이전트를 그대로 실행해 `C:\Users\wnsdu\OneDrive\대시보드\video_status.json`에 `task`, `progress_pct`, `done`, `total`을 쓰는 경우에도 대시보드가 이를 읽어 현재 프로세스 진행률로 변환한다.

```json
{
  "status": "active",
  "progress": 0,
  "current_file": "",
  "batch_progress": 0,
  "total_files": 0,
  "completed_files": [],
  "current_step": 0,
  "current_process": "raw_transcribe",
  "current_process_label": "원본 전사",
  "message": "",
  "speaker_count": 2,
  "preset": "fast",
  "preview_file": "",
  "model_status": {
    "active_model": "large-v3",
    "device": "cuda",
    "compute": "float16",
    "task": "원본 전사"
  },
  "model_download": {
    "name": "large-v3",
    "status": "downloading",
    "progress": 42
  },
  "process_status": {
    "raw_transcribe": { "status": "active", "progress": 30 },
    "diarize": { "status": "pending", "progress": 0 }
  },
  "process_results": {
    "raw_transcribe": [
      { "title": "원본 SRT", "path": "H:/Q&A/sample_raw.srt" }
    ]
  }
}
```

실패 시에는 `status`를 `error`로 두고, 어떤 파일에서 왜 실패했는지 `message`에 남긴다.

## Process Status IDs

대시보드는 아래 ID를 기준으로 현재 프로세스를 표시한다. 단계가 바뀔 때마다 `current_process`를 갱신한다.

- `raw_transcribe`: 원본 전사
- `diarize`: 화자분리
- `transcript_quality_review`: 전사 품질검토
- `crata_term_correction`: CRATA 용어 교정
- `speaker_review`: 화자분리 검토
- `subtitle_preview_review`: 미리보기 검수
- `burnin`: 자막 하드코딩
- `final_encode`: 최종 인코딩

각 단계 상태는 `process_status.<id>.status`에 `pending`, `active`, `waiting`, `done`, `error` 중 하나로 기록한다.
미리보기 확인 대기 중에는 `status: "waiting_preview_review"`와 `current_process: "subtitle_preview_review"`를 같이 기록한다.
각 단계의 산출물은 `process_results.<id>`에 `{ "title": "...", "path": "...", "note": "..." }` 형태로 기록한다.
전사 품질검토, CRATA 용어 교정, 화자분리 검토에서 수정/확인 항목이 있으면 `changes` 배열에 `{ "before": "기존", "after": "변경", "reason": "근거", "segment": "시간대" }` 형태로 남긴다. 대시보드는 이를 보기 패널 아래에서 `기존 -> 변경` 카드로 표시한다.
전사·화자분리·품질검토·용어 교정 결과는 사용자가 대시보드에서 바로 열어볼 수 있어야 한다. 텍스트 산출물에는 `viewer`를 함께 남긴다.
화자분리 검토 단계에는 사용자가 크게 읽을 최종 전사록을 반드시 함께 남긴다.

```json
{
  "process_results": {
    "raw_transcribe": [
      { "title": "원본 전사록", "path": "H:/Q&A/sample.srt", "kind": "전사록", "viewer": "transcript" }
    ],
    "diarize": [
      { "title": "화자분리 ASS 자막", "path": "H:/Q&A/sample.ass", "kind": "화자분리", "viewer": "diarized" }
    ],
    "transcript_quality_review": [
      {
        "title": "전사 품질검토 리포트",
        "path": "H:/Q&A/sample_review.md",
        "kind": "품질검토",
        "viewer": "review",
        "changes": [
          { "before": "오인식된 문장", "after": "수정 후보 문장", "reason": "문맥상 어색한 전사", "segment": "00:02:10" }
        ]
      }
    ],
    "crata_term_correction": [
      {
        "title": "CRATA 용어 교정",
        "path": "H:/Q&A/sample_term_correction.md",
        "kind": "용어 교정",
        "viewer": "changes",
        "changes": [
          { "before": "기존 문장", "after": "변경 문장", "reason": "CRATA 공식 용어 기준", "segment": "00:01:23" }
        ]
      }
    ],
    "speaker_review": [
      { "title": "최종 검토 전사록", "path": "H:/Q&A/sample_final_reviewed.srt", "kind": "최종 전사록", "viewer": "transcript" },
      {
        "title": "화자분리 검토 리포트",
        "path": "H:/Q&A/sample_speaker_review.md",
        "kind": "화자분리 검토",
        "viewer": "review",
        "changes": [
          { "before": "질문자", "after": "강사", "reason": "문맥상 설명 발화", "segment": "00:04:32" }
        ]
      }
    ]
  }
}
```

GPU 모델 다운로드가 진행되면 `model_download.status`를 `downloading`, `progress`를 0-100으로 갱신하고, 완료 후 `completed`로 바꾼다.

## Expected Outputs

- `.srt`: 원본 전사 자막
- `_reviewed.srt` 또는 `_reviewed.md`: 검토/용어 교정본
- `.ass`: 화자별 스타일 자막
- `_colored.srt`: 화자 색상 확인용 자막
- `_final_reviewed.srt` 또는 `_speaker_reviewed.srt`: 화자분리 검토까지 반영한 최종 전사록
- `_preview.mp4` 또는 `preview_*.mp4`: 자막 크기/위치 검수용 미리보기
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
- 최종 인코딩 전에 미리보기 검수를 만들지 못한 경우
- 미리보기 승인 없이 최종 인코딩을 진행하려는 경우

실패 원인과 필요한 조치를 Codex Output에 남긴다.
