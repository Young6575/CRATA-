#!/usr/bin/env python3
"""CRATA Dashboard Local Server
실행: python server.py
브라우저: http://localhost:8765
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json, re, glob, pathlib, mimetypes, os, shutil, socket, subprocess, tempfile, threading, time, uuid, queue
import sys
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, quote

ROOT = pathlib.Path(__file__).parent
TASK_LOCK = threading.Lock()
MAX_OUTPUT_CHARS = 120000
COVERAGE_CACHE = {'signature': None, 'data': None}
COVERAGE_PARSER_VERSION = 4
CATEGORY_FILE = ROOT / '처리관리' / 'categories.json'
MEETINGS_FILE = ROOT / '처리관리' / '녹음' / 'plaud_meetings.json'
PROCESSED_FILE = ROOT / '처리관리' / '녹음' / 'plaud_processed.json'
TRANSCRIPTS_DIR = ROOT / '처리관리' / '녹음' / 'transcripts'
CALENDAR_FILE = ROOT / '처리관리' / 'calendar_events.json'
LOCAL_SETTINGS_FILE = ROOT / '처리관리' / 'local_settings.json'
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.mts', '.m2ts', '.mxf'}
VIDEO_EDIT_QUEUE_FILE = ROOT / '처리관리' / 'video_edit_queue.json'
VIDEO_STATUS_FILE = ROOT / 'video_status.json'
VIDEO_SYSTEM_CACHE = {'at': 0, 'data': {}}
VIDEO_TEXT_ARTIFACT_EXTENSIONS = {'.srt', '.ass', '.vtt', '.txt', '.md', '.json', '.csv', '.log'}
VIDEO_ARTIFACT_MAX_BYTES = 700000
DEFAULT_VIDEO_SUBTITLE_STYLE = {
    'font_name': 'Malgun Gothic',
    'font_size': 54,
    'bold': True,
    'primary_color': '#FFFFFF',
    'outline_color': '#000000',
    'outline': 3,
    'shadow': 0,
    'margin_v': 74,
    'speaker_labels': False,
    'speaker_colors': False,
}
VIDEO_PROCESS_STEPS = [
    {'id': 'raw_transcribe', 'label': '원본 전사', 'desc': 'large-v3로 원본 SRT/전사본 생성'},
    {'id': 'diarize', 'label': '화자분리', 'desc': '입력한 화자 수 기준으로 화자 라벨 부여'},
    {'id': 'transcript_quality_review', 'label': '전사 품질검토', 'desc': '오인식, 끊김, 반복, 어색한 문장 확인'},
    {'id': 'crata_term_correction', 'label': 'CRATA 용어 교정', 'desc': '지식/결과지문구 기준으로 공식 용어 정리'},
    {'id': 'speaker_review', 'label': '화자분리 검토', 'desc': '강사/질문자 라벨 뒤바뀜과 짧은 발화 확인'},
    {'id': 'subtitle_preview_review', 'label': '미리보기 검수', 'desc': '자막 크기, 위치, 색상, 가림 여부 확인 후 승인 필요'},
    {'id': 'burnin', 'label': '자막 하드코딩', 'desc': '승인된 자막을 영상에 합성'},
    {'id': 'final_encode', 'label': '최종 인코딩', 'desc': '승인 후 전체 길이 최종 렌더링'},
]
VIDEO_PROCESS_ALIASES = {
    'transcribe': 'raw_transcribe',
    'transcription': 'raw_transcribe',
    'diarization': 'diarize',
    'review': 'transcript_quality_review',
    'quality_review': 'transcript_quality_review',
    'crata_review': 'crata_term_correction',
    'crata_term_review': 'crata_term_correction',
    'speaker_check': 'speaker_review',
    'preview': 'subtitle_preview_review',
    'preview_review': 'subtitle_preview_review',
    'waiting_preview_review': 'subtitle_preview_review',
    'hardcode': 'burnin',
    'encode': 'final_encode',
    'final': 'final_encode',
}
VIDEO_STATUS_SNAPSHOT_KEYS = {
    'status', 'progress', 'batch_progress', 'total_files', 'completed_files',
    'current_step', 'current_process', 'current_process_label', 'process_steps',
    'process_status', 'process_results', 'artifacts', 'message', 'preview_file',
    'source_path', 'original_source_path', 'current_file', 'workspace_root',
    'workspace_path', 'moved_files', 'prepared_files', 'speaker_count',
}

DEFAULT_CATEGORIES = [
    {'id': 'phrase-edit', 'name': '문구수정', 'color': 'blue', 'keywords': ['문구', '결과지', '카피', '표현', '윤문', 'revise', 'phrase']},
    {'id': 'knowledge-edit', 'name': '지식수정', 'color': 'green', 'keywords': ['지식', '정의', '유형', '조합', '검사개요', 'knowledge']},
    {'id': 'homepage-design', 'name': '홈페이지 디자인 수정', 'color': 'purple', 'keywords': ['홈페이지', '대시보드', '관제', '디자인', '화면', 'ui', 'ux', '프론트', 'dashboard']},
    {'id': 'meeting-minutes', 'name': '회의록 처리', 'color': 'yellow', 'keywords': ['회의록', 'plaud', '녹음', '전사', 'transcript', '회의']},
    {'id': 'review-docs', 'name': '검수/문서화', 'color': 'blue', 'keywords': ['검수', 'docx', '문서', '대표님', '리뷰', 'export', 'apply']},
    {'id': 'media-work', 'name': '영상/미디어', 'color': 'red', 'keywords': ['영상', '자막', '인코딩', '미디어', 'video']},
    {'id': 'system-tools', 'name': '시스템/도구', 'color': 'green', 'keywords': ['서버', 'api', 'codex', 'claude', '브리지', '자동화', '도구', 'cli']},
    {'id': 'ops-planning', 'name': '운영/기획', 'color': 'yellow', 'keywords': ['기획', '운영', '보고', '일정', '계획', '프로그램']},
]


def now_iso():
    return datetime.now().isoformat()


def clock_time():
    return datetime.now().strftime('%H:%M:%S')


def resolve_claude_command():
    """Windows prefers the .cmd shim when Claude is installed through npm/nvm."""
    return shutil.which('claude.cmd') or shutil.which('claude') or 'claude'


def resolve_codex_command():
    """Use the npm/nvm shim before the WindowsApps desktop shim."""
    return shutil.which('codex.cmd') or shutil.which('codex') or 'codex'


def resolve_npx_command():
    """Resolve npx in the same environment used by dashboard worker CLIs."""
    return shutil.which('npx.cmd') or shutil.which('npx') or 'npx'


def plaud_mcp_config_json():
    return json.dumps({
        'mcpServers': {
            'plaud': {
                'command': resolve_npx_command(),
                'args': ['-y', '@plaud-ai/mcp@latest'],
            },
        },
    }, ensure_ascii=False)


def read_json_file(fpath, fallback=None):
    try:
        return json.loads(pathlib.Path(fpath).read_text(encoding='utf-8'))
    except Exception:
        return fallback if fallback is not None else {}


def write_json_file(fpath, data):
    pathlib.Path(fpath).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(fpath).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def read_local_settings():
    data = read_json_file(LOCAL_SETTINGS_FILE, {})
    return data if isinstance(data, dict) else {}


def write_local_settings(settings):
    data = settings if isinstance(settings, dict) else {}
    write_json_file(LOCAL_SETTINGS_FILE, data)
    return data


def clamp_number(value, default, minimum, maximum):
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return max(minimum, min(maximum, num))


def normalize_hex_color(value, default='#FFFFFF'):
    raw = str(value or '').strip()
    if re.match(r'^#[0-9A-Fa-f]{6}$', raw):
        return raw.upper()
    return default


def normalize_video_subtitle_style(raw=None):
    raw = raw if isinstance(raw, dict) else {}
    style = {**DEFAULT_VIDEO_SUBTITLE_STYLE}
    style.update({
        'font_name': str(raw.get('font_name') or raw.get('fontName') or style['font_name']).strip()[:80] or style['font_name'],
        'font_size': int(clamp_number(raw.get('font_size') or raw.get('fontSize'), style['font_size'], 28, 96)),
        'bold': bool(raw.get('bold', style['bold'])),
        'primary_color': normalize_hex_color(raw.get('primary_color') or raw.get('primaryColor'), style['primary_color']),
        'outline_color': normalize_hex_color(raw.get('outline_color') or raw.get('outlineColor'), style['outline_color']),
        'outline': round(clamp_number(raw.get('outline'), style['outline'], 0, 8), 1),
        'shadow': round(clamp_number(raw.get('shadow'), style['shadow'], 0, 6), 1),
        'margin_v': int(clamp_number(raw.get('margin_v') or raw.get('marginV'), style['margin_v'], 20, 180)),
        'speaker_labels': False,
        'speaker_colors': False,
    })
    style['primary_color'] = '#FFFFFF'
    return style


def read_video_subtitle_style():
    settings = read_local_settings()
    video_settings = settings.get('video') if isinstance(settings.get('video'), dict) else {}
    return normalize_video_subtitle_style(video_settings.get('subtitle_style'))


def save_video_subtitle_style(data):
    style = normalize_video_subtitle_style(data.get('style') if isinstance(data.get('style'), dict) else data)
    settings = read_local_settings()
    video_settings = settings.get('video') if isinstance(settings.get('video'), dict) else {}
    video_settings['subtitle_style'] = style
    settings['video'] = video_settings
    write_local_settings(settings)
    return {'ok': True, 'style': style}


def parse_port(value, default=8765):
    try:
        port = int(value)
        return port if 0 < port < 65536 else default
    except Exception:
        return default


def server_bind_config():
    settings = read_local_settings()
    server_settings = settings.get('server') if isinstance(settings.get('server'), dict) else {}
    host = os.environ.get('CRATA_HOST') or server_settings.get('host') or '127.0.0.1'
    port = parse_port(os.environ.get('CRATA_PORT') or server_settings.get('port'), 8765)
    return str(host), port


def local_ipv4_addresses():
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr and not addr.startswith('127.'):
                addresses.add(addr)
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('8.8.8.8', 80))
            addr = sock.getsockname()[0]
            if addr and not addr.startswith('127.'):
                addresses.add(addr)
    except Exception:
        pass
    return sorted(addresses)


def run_git_command(args, timeout=120):
    try:
        proc = subprocess.run(
            ['git', *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or '').strip(), (proc.stderr or '').strip()
    except Exception as exc:
        return 1, '', str(exc)


def git_untracked_files():
    code, out, _ = run_git_command(['ls-files', '--others', '--exclude-standard'], timeout=30)
    if code != 0:
        return []
    return [line.strip().replace('\\', '/') for line in out.splitlines() if line.strip()]


def git_upstream_ref():
    code, out, _ = run_git_command(['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'], timeout=30)
    return out.strip() if code == 0 else ''


def git_incoming_files(upstream):
    if not upstream:
        return []
    code, out, _ = run_git_command(['diff', '--name-only', f'HEAD..{upstream}'], timeout=30)
    if code != 0:
        return []
    return [line.strip().replace('\\', '/') for line in out.splitlines() if line.strip()]


def backup_untracked_incoming_files(incoming_files):
    untracked = set(git_untracked_files())
    conflicts = [path for path in incoming_files if path in untracked]
    if not conflicts:
        return []

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_root = ROOT / '처리관리' / 'backups' / 'untracked-update' / stamp
    backups = []
    for rel in conflicts:
        source = ROOT / rel
        if not source.is_file():
            continue
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
        backups.append({'path': rel, 'backup': str(dest.relative_to(ROOT))})
    return backups


def update_from_git():
    if not (ROOT / '.git').exists():
        return {'ok': False, 'error': '이 폴더는 Git 저장소가 아닙니다.'}

    code, status_out, status_err = run_git_command(['status', '--porcelain', '--untracked-files=no'], timeout=30)
    if code != 0:
        return {'ok': False, 'error': status_err or status_out or 'Git 상태 확인에 실패했습니다.'}

    _, before, _ = run_git_command(['rev-parse', 'HEAD'], timeout=30)
    fetch_code, fetch_out, fetch_err = run_git_command(['fetch'], timeout=180)
    if fetch_code != 0:
        return {'ok': False, 'error': fetch_err or fetch_out or 'GitHub 변경사항 확인에 실패했습니다.'}
    upstream = git_upstream_ref()
    auto_backups = backup_untracked_incoming_files(git_incoming_files(upstream))
    local_changes = [line.strip() for line in status_out.splitlines() if line.strip()]
    pull_args = ['pull', '--ff-only']
    if local_changes:
        pull_args.append('--autostash')
    code, pull_out, pull_err = run_git_command(pull_args, timeout=180)
    _, after, _ = run_git_command(['rev-parse', 'HEAD'], timeout=30)

    if code != 0:
        if local_changes and 'autostash' in (pull_err + pull_out).lower():
            return {
                'ok': False,
                'local_changes': True,
                'error': '로컬 수정 자동 보관을 지원하지 않는 Git 환경입니다. 데스크탑에서 한 번 수동 업데이트가 필요합니다.',
                'details': status_out,
            }
        return {
            'ok': False,
            'error': pull_err or pull_out or 'git pull에 실패했습니다.',
            'details': pull_out,
            'local_changes': bool(local_changes),
            'local_change_files': local_changes,
            'untracked_backups': auto_backups,
        }

    changed_files = []
    if before and after and before != after:
        _, diff_out, _ = run_git_command(['diff', '--name-only', before, after], timeout=30)
        changed_files = [line.strip() for line in diff_out.splitlines() if line.strip()]

    server_restart_required = any(
        path == 'server.py' or path.startswith('scripts/')
        for path in changed_files
    )
    browser_reload_required = any(
        path == 'dashboard.html'
        for path in changed_files
    )

    return {
        'ok': True,
        'updated': bool(before and after and before != after),
        'before': before,
        'after': after,
        'changed_files': changed_files,
        'local_changes_autostashed': bool(local_changes),
        'local_change_files': local_changes,
        'untracked_backups': auto_backups,
        'server_restart_required': server_restart_required,
        'browser_reload_required': browser_reload_required,
        'message': pull_out or 'Already up to date.',
    }


def spawn_replacement_server():
    launcher = f"""
import os, socket, subprocess, time
repo = {json.dumps(str(ROOT))}
python = {json.dumps(sys.executable)}
port = int(os.environ.get('CRATA_PORT') or '8765')
deadline = time.time() + 20
while time.time() < deadline:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        sock.connect(('127.0.0.1', port))
        sock.close()
        time.sleep(0.5)
    except OSError:
        try:
            sock.close()
        except Exception:
            pass
        break
flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
subprocess.Popen([python, 'server.py'], cwd=repo, env=os.environ.copy(), creationflags=flags)
"""
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    subprocess.Popen(
        [sys.executable, '-c', launcher],
        cwd=str(ROOT),
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )


def schedule_server_restart(httpd, delay=1.2):
    def restart():
        try:
            spawn_replacement_server()
        finally:
            httpd.shutdown()

    timer = threading.Timer(delay, restart)
    timer.daemon = True
    timer.start()
    return True


def video_browser_roots():
    settings = read_local_settings()
    video_settings = settings.get('video') if isinstance(settings.get('video'), dict) else {}
    roots = []
    seen = set()

    def add_root(label, path_value):
        if not path_value:
            return
        try:
            path = pathlib.Path(str(path_value)).expanduser()
            resolved = path.resolve()
            key = str(resolved).lower()
            if key in seen or not resolved.exists():
                return
            seen.add(key)
            roots.append({
                'type': 'folder',
                'name': label,
                'path': str(resolved),
                'meta': str(resolved),
            })
        except Exception:
            return

    source_dirs = video_settings.get('source_dirs') if isinstance(video_settings.get('source_dirs'), list) else []
    for idx, source_dir in enumerate(source_dirs, start=1):
        add_root(f'영상 소스 {idx}', source_dir)
    add_root('영상 작업 폴더', video_settings.get('workspace_dir'))
    add_root('렌더 출력 폴더', video_settings.get('render_output_dir'))

    home = pathlib.Path.home()
    add_root('내 동영상', home / 'Videos')
    add_root('바탕화면', home / 'Desktop')
    add_root('다운로드', home / 'Downloads')

    if os.name == 'nt':
        for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
            add_root(f'{letter}: 드라이브', f'{letter}:\\')
    else:
        add_root('파일 시스템', '/')

    return roots


def safe_file_meta(path):
    try:
        stat = path.stat()
        return {
            'size': stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }
    except Exception:
        return {'size': 0, 'modified': ''}


def browse_video_files(raw_path=''):
    raw_path = str(raw_path or '').strip()
    if not raw_path:
        return {
            'ok': True,
            'mode': 'roots',
            'current': '',
            'parent': '',
            'items': video_browser_roots(),
            'extensions': sorted(VIDEO_EXTENSIONS),
        }

    try:
        target = pathlib.Path(raw_path).expanduser().resolve()
    except Exception as exc:
        return {'ok': False, 'error': f'경로를 해석할 수 없습니다: {exc}', 'items': []}

    if not target.exists():
        return {'ok': False, 'error': '해당 경로가 존재하지 않습니다.', 'current': str(target), 'items': []}

    selected_file = ''
    if target.is_file():
        selected_file = str(target)
        target = target.parent

    if not target.is_dir():
        return {'ok': False, 'error': '폴더를 열 수 없습니다.', 'current': str(target), 'items': []}

    items = []
    try:
        children = list(target.iterdir())
    except Exception as exc:
        return {'ok': False, 'error': f'폴더 목록을 읽을 수 없습니다: {exc}', 'current': str(target), 'items': []}

    directories = []
    files = []
    for child in children:
        name = child.name
        if name.startswith('$') or name.startswith('.') or name in {'System Volume Information', '$RECYCLE.BIN'}:
            continue
        try:
            if child.is_dir():
                meta = safe_file_meta(child)
                directories.append({
                    'type': 'folder',
                    'name': name,
                    'path': str(child),
                    'meta': '폴더',
                    **meta,
                })
            elif child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                meta = safe_file_meta(child)
                files.append({
                    'type': 'file',
                    'name': name,
                    'path': str(child),
                    'meta': child.suffix.lower().lstrip('.').upper(),
                    **meta,
                })
        except Exception:
            continue

    directories.sort(key=lambda item: item['name'].lower())
    files.sort(key=lambda item: item['name'].lower())
    items = (directories + files)[:1000]

    parent = ''
    try:
        if target.parent != target:
            parent = str(target.parent)
    except Exception:
        parent = ''

    return {
        'ok': True,
        'mode': 'browse',
        'current': str(target),
        'parent': parent,
        'selected': selected_file,
        'items': items,
        'extensions': sorted(VIDEO_EXTENSIONS),
    }


def resolve_video_preview_path(raw_path=''):
    raw_path = str(raw_path or '').strip()
    if not raw_path:
        return None, '영상 경로가 비어 있습니다.'
    try:
        target = pathlib.Path(raw_path).expanduser().resolve()
    except Exception as exc:
        return None, f'경로를 해석할 수 없습니다: {exc}'
    if not target.exists():
        return None, '해당 영상 경로가 존재하지 않습니다.'
    if target.is_dir():
        candidates = sorted(
            [p for p in target.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS],
            key=lambda p: p.name.lower(),
        )
        if not candidates:
            return None, '이 폴더에는 미리보기 가능한 영상 파일이 없습니다.'
        target = candidates[0]
    if not target.is_file() or target.suffix.lower() not in VIDEO_EXTENSIONS:
        return None, '미리보기는 영상 파일만 지원합니다.'
    return target, ''


def collect_video_source_files(raw_path='', max_files=5000):
    raw_path = str(raw_path or '').strip()
    if not raw_path:
        return [], '영상 경로가 비어 있습니다.'
    try:
        target = pathlib.Path(raw_path).expanduser().resolve()
    except Exception as exc:
        return [], f'경로를 해석할 수 없습니다: {exc}'
    if not target.exists():
        return [], '해당 영상 경로가 존재하지 않습니다.'
    if target.is_file():
        if target.suffix.lower() not in VIDEO_EXTENSIONS:
            return [], '지원하지 않는 영상 파일 형식입니다.'
        return [target], ''
    if not target.is_dir():
        return [], '영상 파일 또는 폴더 경로를 입력하세요.'

    files = []
    try:
        for child in target.rglob('*'):
            if len(files) >= max_files:
                break
            if any(part.startswith('$') or part.startswith('.') for part in child.parts):
                continue
            if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                files.append(child)
    except Exception as exc:
        return [], f'폴더 안의 영상 파일을 읽을 수 없습니다: {exc}'

    files.sort(key=lambda p: str(p).lower())
    if not files:
        return [], '이 폴더에는 처리 가능한 영상 파일이 없습니다.'
    return files, ''


def video_upload_dir():
    settings = read_local_settings()
    video_settings = settings.get('video') if isinstance(settings.get('video'), dict) else {}
    configured = video_settings.get('upload_dir') or video_settings.get('workspace_dir')
    if configured:
        try:
            path = pathlib.Path(str(configured)).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return path.resolve()
        except Exception:
            pass
    path = ROOT / '처리관리' / 'video_uploads'
    path.mkdir(parents=True, exist_ok=True)
    return path


def video_workspace_dir():
    settings = read_local_settings()
    video_settings = settings.get('video') if isinstance(settings.get('video'), dict) else {}
    configured = video_settings.get('workspace_dir') or video_settings.get('work_dir')
    if configured:
        try:
            path = pathlib.Path(str(configured)).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return path.resolve()
        except Exception:
            pass
    path = ROOT / '처리관리' / 'video_workspaces'
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_upload_filename(filename):
    name = pathlib.Path(str(filename or 'upload.mp4')).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', name).strip(' ._')
    return name or f'upload_{int(time.time())}.mp4'


def unique_upload_path(filename):
    upload_dir = video_upload_dir()
    safe_name = safe_upload_filename(filename)
    stem = pathlib.Path(safe_name).stem or 'upload'
    suffix = pathlib.Path(safe_name).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        suffix = '.mp4'
    candidate = upload_dir / f'{stem}{suffix}'
    if not candidate.exists():
        return candidate
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return upload_dir / f'{stem}_{stamp}{suffix}'


def normalize_video_source_key(path_value):
    return str(path_value or '').replace('\\', '/').strip().lower()


def video_item_title(source_path, title=''):
    if title:
        return str(title).strip()
    name = pathlib.Path(str(source_path or '')).name
    return name or '영상 편집 작업'


def make_video_edit_item(source_path, title='', source='manual', status='pending_edit', note=''):
    return {
        'id': f'vedit_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}',
        'title': video_item_title(source_path, title),
        'source_path': str(source_path or '').strip(),
        'status': status,
        'source': source,
        'note': str(note or '').strip(),
        'created_at': now_iso(),
        'updated_at': now_iso(),
        'cut_start': '',
        'cut_end': '',
        'layout': 'pip',
    }


def video_status_file_mtime(fpath):
    try:
        return pathlib.Path(fpath).stat().st_mtime
    except Exception:
        return 0


def configured_video_status_files():
    paths = []
    settings = read_local_settings()
    video_settings = settings.get('video') if isinstance(settings.get('video'), dict) else {}

    for key in ('status_file', 'external_status_file', 'legacy_status_file'):
        value = video_settings.get(key)
        if value:
            paths.append(value)

    status_files = video_settings.get('status_files')
    if isinstance(status_files, list):
        paths.extend(status_files)

    env_paths = os.environ.get('CRATA_VIDEO_STATUS_FILES') or os.environ.get('CRATA_VIDEO_STATUS_FILE')
    if env_paths:
        paths.extend(part for part in env_paths.split(os.pathsep) if part.strip())

    paths.append(pathlib.Path.home() / 'OneDrive' / '대시보드' / 'video_status.json')

    unique = []
    seen = set()
    for raw in paths:
        try:
            fpath = pathlib.Path(raw).expanduser()
        except Exception:
            continue
        key = str(fpath).lower()
        if key == str(VIDEO_STATUS_FILE).lower() or key in seen:
            continue
        seen.add(key)
        unique.append(fpath)
    return unique


def normalize_legacy_video_status(data, legacy_source=False):
    status = dict(data or {})
    progress_pct = status.get('progress_pct')
    progress = status.get('progress')
    try:
        pct = float(progress_pct if progress_pct is not None else (progress or 0))
    except (TypeError, ValueError):
        pct = 0

    if progress_pct is not None and (legacy_source or progress in (None, '', 0)):
        status['progress'] = pct

    if not status.get('total_files') and status.get('total') is not None:
        status['total_files'] = status.get('total') or 0
    if not status.get('completed_files') and isinstance(status.get('completed'), list):
        status['completed_files'] = status.get('completed')

    completed_files = status.get('completed_files') if isinstance(status.get('completed_files'), list) else []
    try:
        total_files = int(status.get('total_files') or status.get('total') or 0)
    except (TypeError, ValueError):
        total_files = 0
    try:
        done_count = int(status.get('done') if status.get('done') is not None else len(completed_files))
    except (TypeError, ValueError):
        done_count = len(completed_files)

    if total_files > 0:
        batch_progress = ((done_count + min(max(pct, 0), 100) / 100) / total_files) * 100
        status['batch_progress'] = round(min(max(batch_progress, 0), 100), 1)

    current_process = infer_current_video_process(status)
    if current_process:
        status['current_process'] = current_process
        process_status = status.get('process_status') if isinstance(status.get('process_status'), dict) else {}
        current_info = process_status.get(current_process) if isinstance(process_status.get(current_process), dict) else {}
        process_status[current_process] = {
            **current_info,
            'status': 'done' if pct >= 100 and (not total_files or done_count >= total_files) else 'active',
            'progress': min(max(pct, 0), 100),
            'message': status.get('current_file') or current_info.get('message') or '',
        }
        current_idx = next((idx for idx, step in enumerate(VIDEO_PROCESS_STEPS) if step['id'] == current_process), -1)
        for idx, step in enumerate(VIDEO_PROCESS_STEPS):
            if idx < current_idx and step['id'] not in process_status:
                process_status[step['id']] = {'status': 'done', 'progress': 100}
        status['process_status'] = process_status

    if legacy_source or (status.get('task') and status.get('status') in (None, '', 'idle', 'requested')):
        finished = pct >= 100 and (not total_files or done_count >= total_files)
        status['status'] = 'done' if finished else 'active'

    return status


def video_status_source_names(status):
    names = set()
    for key in ('source_path', 'current_file', 'path', 'videoSourcePath'):
        value = str(status.get(key) or '').strip()
        if not value:
            continue
        name = re.split(r'[\\/]+', value)[-1].strip().lower()
        if name:
            names.add(name)
    return names


def video_status_refers_to_same_source(base, candidate):
    base_names = video_status_source_names(base)
    candidate_names = video_status_source_names(candidate)
    if base_names and candidate_names and base_names.intersection(candidate_names):
        return True
    base_text = ' '.join(str(base.get(key) or '') for key in ('source_path', 'current_file')).lower()
    candidate_text = ' '.join(str(candidate.get(key) or '') for key in ('source_path', 'current_file')).lower()
    return any(name and name in candidate_text for name in base_names) or any(name and name in base_text for name in candidate_names)


def video_result_entry(title, path, process, kind='', viewer='', **extra):
    item = {
        'title': title,
        'path': str(path),
        'process': process,
        'kind': kind or title,
        'viewer': viewer or 'file',
    }
    item.update({k: v for k, v in extra.items() if v not in (None, '')})
    return item


def normalize_process_results(value):
    return value if isinstance(value, dict) else {}


def add_process_result_entry(process_results, process_id, entry):
    if not entry or not entry.get('path'):
        return
    items = process_results.get(process_id)
    if not isinstance(items, list):
        items = [items] if items else []
    target_path = str(entry.get('path') or '').lower()
    if not any(str((item or {}).get('path') if isinstance(item, dict) else item).lower() == target_path for item in items):
        items.append(entry)
    process_results[process_id] = items


def video_lookup_token(value):
    text = str(value or '').strip()
    if not text:
        return ''
    name = re.split(r'[\\/]+', text)[-1]
    stem = pathlib.PurePath(name).stem or name
    return re.sub(r'[\W_]+', '', stem.lower(), flags=re.UNICODE)


def find_video_task_workspace_path(task, max_items=5000):
    root_raw = task.get('videoWorkspaceRoot') or task.get('workspace_root') or ''
    if not root_raw:
        return ''
    try:
        root = pathlib.Path(str(root_raw)).expanduser().resolve()
    except Exception:
        return ''
    if not root.exists():
        return ''
    if root.is_file():
        return str(root.parent)

    source_token = video_lookup_token(task.get('videoSourcePath') or task.get('source_path') or task.get('title'))
    if not source_token:
        return str(root)

    matches = []
    scanned = 0
    try:
        for child in root.rglob('*'):
            scanned += 1
            if scanned > max_items:
                break
            try:
                if child.is_dir():
                    if source_token in video_lookup_token(child.name):
                        matches.append(child)
                elif child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                    if source_token in video_lookup_token(child.name):
                        matches.append(child.parent)
            except Exception:
                continue
    except Exception:
        return ''

    if not matches:
        return ''
    unique = {}
    for path in matches:
        unique[str(path).lower()] = path
    ordered = list(unique.values())
    ordered.sort(key=lambda path: video_status_file_mtime(path), reverse=True)
    return str(ordered[0])


def video_source_paths_from_status(status):
    raw_paths = []
    for key in ('source_path', 'videoSourcePath', 'current_file', 'workspace_path', 'workspace_root'):
        value = str(status.get(key) or '').strip()
        if value:
            raw_paths.append(value)
    for key in ('completed_files', 'completed'):
        values = status.get(key)
        if isinstance(values, list):
            raw_paths.extend(str(value or '').strip() for value in values if value)

    paths = []
    seen = set()
    for raw in raw_paths:
        raw = raw.replace('/', os.sep)
        fpath = pathlib.Path(raw)
        if not fpath.is_absolute():
            continue
        if fpath.is_dir():
            try:
                candidates, _ = collect_video_source_files(str(fpath))
            except Exception:
                candidates = []
            for candidate in candidates[:20]:
                key = str(candidate).lower()
                if key not in seen:
                    seen.add(key)
                    paths.append(candidate)
            continue
        if fpath.suffix.lower() in VIDEO_EXTENSIONS:
            key = str(fpath).lower()
            if key not in seen:
                seen.add(key)
                paths.append(fpath)
    return paths


def enrich_video_process_results(status):
    process_results = normalize_process_results(status.get('process_results'))
    for video in video_source_paths_from_status(status):
        stem = video.stem
        folder = video.parent
        sidecars = [
            ('raw_transcribe', '원본 전사록', video.with_suffix('.srt'), 'transcript', 'transcript'),
            ('diarize', '화자분리 ASS 자막', video.with_suffix('.ass'), 'speaker subtitle', 'diarized'),
            ('diarize', '화자분리 색상 SRT', folder / f'{stem}_colored.srt', 'speaker subtitle', 'diarized'),
            ('transcript_quality_review', '전사 품질검토 리포트', folder / f'{stem}_review.md', 'quality review', 'review'),
            ('transcript_quality_review', '전사 품질검토 리포트', folder / f'{stem}_quality_review.md', 'quality review', 'review'),
            ('crata_term_correction', 'CRATA 용어 교정 리포트', folder / f'{stem}_term_correction.md', 'term correction', 'changes'),
            ('crata_term_correction', 'CRATA 용어 교정본', folder / f'{stem}_reviewed.srt', 'corrected transcript', 'transcript'),
            ('speaker_review', '최종 검토 전사록', folder / f'{stem}_final_reviewed.srt', 'final reviewed transcript', 'transcript'),
            ('speaker_review', '화자분리 검토 전사록', folder / f'{stem}_speaker_reviewed.srt', 'speaker reviewed transcript', 'transcript'),
            ('speaker_review', '화자분리 검토 ASS', folder / f'{stem}_reviewed.ass', 'speaker reviewed subtitle', 'diarized'),
            ('speaker_review', '화자분리 검토 리포트', folder / f'{stem}_speaker_review.md', 'speaker review', 'review'),
            ('subtitle_preview_review', '자막 미리보기', folder / f'{stem}_preview.mp4', 'preview', 'video'),
            ('burnin', '자막 하드코딩 결과', folder / f'{stem}_sub.mp4', 'burned video', 'video'),
            ('final_encode', '최종 인코딩 결과', folder / f'{stem}_final.mp4', 'final video', 'video'),
        ]
        for process_id, title, artifact, kind, viewer in sidecars:
            if artifact.exists():
                add_process_result_entry(process_results, process_id, video_result_entry(title, artifact, process_id, kind, viewer))
    status['process_results'] = process_results
    return status


def read_video_status():
    base = read_json_file(VIDEO_STATUS_FILE, {})
    base = base if isinstance(base, dict) else {}
    base_mtime = video_status_file_mtime(VIDEO_STATUS_FILE)
    if base:
        base['_status_source'] = str(VIDEO_STATUS_FILE)
        base['_status_mtime'] = base_mtime
        base['_status_source_kind'] = 'project'

    newest_external = {}
    newest_external_mtime = 0
    for fpath in configured_video_status_files():
        data = read_json_file(fpath, {})
        if not isinstance(data, dict) or not data:
            continue
        mtime = video_status_file_mtime(fpath)
        if mtime >= newest_external_mtime:
            newest_external_mtime = mtime
            newest_external = {
                **data,
                '_status_source': str(fpath),
                '_status_mtime': mtime,
                '_status_source_kind': 'legacy',
            }

    external_has_progress = newest_external.get('task') or newest_external.get('progress_pct') is not None
    same_source = video_status_refers_to_same_source(base, newest_external) if base and newest_external else False
    base_waiting_for_runner = base.get('status') in ('idle', 'requested', 'active', None, '')
    external_recent_enough = newest_external_mtime >= max(base_mtime - 2, 0)
    if newest_external and (not base or newest_external_mtime >= base_mtime or (external_has_progress and same_source and base_waiting_for_runner and external_recent_enough)):
        merged = {**base, **newest_external}
        return normalize_legacy_video_status(merged, legacy_source=True)

    return normalize_legacy_video_status(base, legacy_source=False)


def normalize_video_process_id(value):
    raw = str(value or '').strip()
    return VIDEO_PROCESS_ALIASES.get(raw, raw)


def infer_current_video_process(status):
    explicit = normalize_video_process_id(
        status.get('current_process')
        or status.get('current_process_id')
        or status.get('current_step_id')
        or status.get('task')
    )
    if explicit:
        return explicit
    if status.get('status') == 'waiting_preview_review':
        return 'subtitle_preview_review'
    try:
        idx = int(status.get('current_step') or 0)
    except (TypeError, ValueError):
        idx = 0
    if 1 <= idx <= len(VIDEO_PROCESS_STEPS):
        return VIDEO_PROCESS_STEPS[idx - 1]['id']
    if status.get('status') in ('requested', 'active'):
        return VIDEO_PROCESS_STEPS[0]['id']
    return ''


def parse_first_number(text):
    m = re.search(r'[-+]?\d+(?:\.\d+)?', str(text or ''))
    return float(m.group(0)) if m else None


def get_windows_memory_metrics():
    if os.name != 'nt':
        return {}
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ('dwLength', ctypes.c_ulong),
                ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        total = stat.ullTotalPhys / (1024 ** 3)
        avail = stat.ullAvailPhys / (1024 ** 3)
        return {
            'usage_percent': float(stat.dwMemoryLoad),
            'used_gb': round(total - avail, 2),
            'total_gb': round(total, 2),
        }
    except Exception:
        return {}


def get_cpu_usage_percent():
    try:
        if os.name == 'nt':
            proc = subprocess.run(
                ['wmic', 'cpu', 'get', 'LoadPercentage', '/value'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            value = parse_first_number(proc.stdout)
            if value is not None:
                return value
        return None
    except Exception:
        return None


def get_gpu_metrics():
    smi = shutil.which('nvidia-smi')
    if not smi:
        return {}
    try:
        proc = subprocess.run(
            [
                smi,
                '--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu',
                '--format=csv,noheader,nounits',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0,
        )
        line = (proc.stdout or '').strip().splitlines()[0] if (proc.stdout or '').strip() else ''
        if not line:
            return {}
        parts = [part.strip() for part in line.split(',')]
        name = parts[0] if parts else ''
        usage = parse_first_number(parts[1] if len(parts) > 1 else '')
        mem_used = parse_first_number(parts[2] if len(parts) > 2 else '')
        mem_total = parse_first_number(parts[3] if len(parts) > 3 else '')
        temp = parse_first_number(parts[4] if len(parts) > 4 else '')
        memory_percent = round((mem_used / mem_total) * 100, 1) if mem_used is not None and mem_total else None
        return {
            'name': name,
            'usage_percent': usage,
            'memory_used_mb': mem_used,
            'memory_total_mb': mem_total,
            'memory_percent': memory_percent,
            'temperature_c': temp,
        }
    except Exception:
        return {}


def collect_system_metrics():
    now = time.time()
    if now - VIDEO_SYSTEM_CACHE.get('at', 0) < 5:
        return VIDEO_SYSTEM_CACHE.get('data', {})
    data = {
        'machine': socket.gethostname(),
        'cpu': {'usage_percent': get_cpu_usage_percent()},
        'ram': get_windows_memory_metrics(),
        'gpu': get_gpu_metrics(),
        'updated_at': now_iso(),
    }
    VIDEO_SYSTEM_CACHE.update({'at': now, 'data': data})
    return data


def video_status_payload():
    status = {
        'status': 'idle',
        'progress': 0,
        'current_file': '',
        'batch_progress': 0,
        'total_files': 0,
        'completed_files': [],
        'current_step': 0,
        'current_process': '',
        'subtitle_style': read_video_subtitle_style(),
        'process_steps': VIDEO_PROCESS_STEPS,
        'process_status': {},
        'system_metrics': collect_system_metrics(),
        'model_status': {
            'active_model': '',
            'device': 'cuda',
            'compute': 'float16',
            'download_status': '대기',
            'download_progress': 0,
        },
        'model_download': {
            'name': '',
            'status': '대기',
            'progress': 0,
        },
        'updated_at': '',
    }
    status.update(read_video_status())
    latest_video_task = {}
    try:
        task_matches = find_video_task_files()
        latest_video_task = task_matches[0][1] if task_matches else {}
        status['video_queue'] = video_queue_payload(current_status=status)
    except Exception:
        latest_video_task = {}
        status['video_queue'] = []
    if latest_video_task:
        task_active = latest_video_task.get('status') in ('pending', 'active') or task_process_alive(latest_video_task)
        try:
            runner_progress = int(latest_video_task.get('progress') or 0)
        except (TypeError, ValueError):
            runner_progress = 0
        status['runner_task'] = {
            'id': latest_video_task.get('id'),
            'title': latest_video_task.get('title'),
            'status': latest_video_task.get('status'),
            'status_label': latest_video_task.get('statusLbl'),
            'progress': runner_progress,
            'pid': latest_video_task.get('pid'),
            'active': bool(task_active),
        }
        if task_active and status.get('status') in ('idle', 'requested', None, ''):
            status['status'] = 'active'
            status['message'] = status.get('message') or 'CLI 에이전트는 실행 중입니다. 영상 처리 세부 진행률을 기다리는 중입니다.'
            current_process = infer_current_video_process(status) or VIDEO_PROCESS_STEPS[0]['id']
            process_status = status.get('process_status') if isinstance(status.get('process_status'), dict) else {}
            current_info = process_status.get(current_process) if isinstance(process_status.get(current_process), dict) else {}
            process_status[current_process] = {
                **current_info,
                'status': current_info.get('status') or 'active',
                'progress': current_info.get('progress', status.get('progress') or 0),
                'message': current_info.get('message') or '세부 진행률 수신 대기',
            }
            status['process_status'] = process_status
    if not status.get('total_files') and status.get('total'):
        status['total_files'] = status.get('total') or 0
    if not status.get('completed_files') and isinstance(status.get('completed'), list):
        status['completed_files'] = status.get('completed')
    if not status.get('process_steps'):
        status['process_steps'] = VIDEO_PROCESS_STEPS
    if status.get('status') == 'idle' and status.get('task'):
        try:
            pct = float(status.get('progress') if status.get('progress') is not None else status.get('progress_pct') or 0)
        except (TypeError, ValueError):
            pct = 0
        status['status'] = 'done' if pct >= 100 else 'active'
    status['current_process'] = infer_current_video_process(status)
    if status.get('status') == 'waiting_preview_review':
        status['current_process'] = 'subtitle_preview_review'
        status['current_process_label'] = '미리보기 확인 대기'
        if status.get('preview_file'):
            process_status = status.get('process_status') if isinstance(status.get('process_status'), dict) else {}
            current_info = process_status.get('subtitle_preview_review') if isinstance(process_status.get('subtitle_preview_review'), dict) else {}
            process_status['subtitle_preview_review'] = {
                **current_info,
                'status': 'waiting',
                'progress': 100,
                'message': current_info.get('message') or '미리보기 파일 생성 완료. 확인 후 승인 대기 중입니다.',
            }
            status['process_status'] = process_status
            status['message'] = status.get('message') or '미리보기 파일 생성 완료. 확인 후 승인하면 최종 인코딩을 진행합니다.'
    enrich_video_process_results(status)
    status['system_metrics'] = collect_system_metrics()
    return status


def inline_content_disposition(filename):
    raw = str(filename or '')
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', pathlib.PurePath(raw).stem).strip('._-') or 'preview'
    suffix = re.sub(r'[^A-Za-z0-9.]+', '', pathlib.PurePath(raw).suffix)[:16]
    fallback = f'{stem}{suffix}' if suffix else stem
    encoded = quote(str(filename or fallback), safe='')
    return f'inline; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def read_video_artifact(raw_path):
    raw = str(raw_path or '').strip()
    if not raw:
        return {'ok': False, 'error': '결과 파일 경로가 없습니다.'}
    fpath = pathlib.Path(raw)
    if not fpath.is_absolute():
        fpath = ROOT / raw
    try:
        fpath = fpath.resolve()
    except Exception:
        pass
    if not fpath.is_file():
        return {'ok': False, 'error': '결과 파일을 찾지 못했습니다.', 'path': str(fpath)}

    ext = fpath.suffix.lower()
    size = fpath.stat().st_size
    if ext not in VIDEO_TEXT_ARTIFACT_EXTENSIONS:
        return {
            'ok': True,
            'path': str(fpath),
            'name': fpath.name,
            'extension': ext,
            'size': size,
            'text': False,
            'content': '',
            'message': '영상 파일은 미리보기 버튼으로 확인하세요.',
        }

    read_bytes = min(size, VIDEO_ARTIFACT_MAX_BYTES)
    with fpath.open('rb') as src:
        blob = src.read(read_bytes)
    content = blob.decode('utf-8-sig', errors='replace')
    return {
        'ok': True,
        'path': str(fpath),
        'name': fpath.name,
        'extension': ext,
        'size': size,
        'text': True,
        'truncated': size > read_bytes,
        'content': content,
    }


def load_video_edit_queue_raw():
    data = read_json_file(VIDEO_EDIT_QUEUE_FILE, {'items': []})
    items = data.get('items') if isinstance(data.get('items'), list) else []
    return data if isinstance(data, dict) else {'items': items}


def write_video_edit_queue(items):
    state = {
        'updated_at': now_iso(),
        'items': items,
    }
    write_json_file(VIDEO_EDIT_QUEUE_FILE, state)
    return state


def merge_video_edit_items(items):
    merged = {}
    order = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source_path = str(item.get('source_path') or item.get('path') or '').strip()
        if not source_path:
            continue
        key = normalize_video_source_key(source_path)
        existing = merged.get(key)
        normalized = {
            'id': item.get('id') or f'vedit_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}',
            'title': video_item_title(source_path, item.get('title') or ''),
            'source_path': source_path,
            'status': item.get('status') or 'pending_edit',
            'source': item.get('source') or 'manual',
            'note': item.get('note') or '',
            'created_at': item.get('created_at') or now_iso(),
            'updated_at': item.get('updated_at') or now_iso(),
            'cut_start': item.get('cut_start') or '',
            'cut_end': item.get('cut_end') or '',
            'layout': item.get('layout') or 'pip',
        }
        if existing:
            current_stamp = task_sort_stamp(existing)
            next_stamp = task_sort_stamp(normalized)
            merged[key] = {**existing, **normalized} if next_stamp >= current_stamp else {**normalized, **existing}
        else:
            merged[key] = normalized
            order.append(key)
    return [merged[key] for key in order if key in merged]


def subtitle_completed_items_from_status():
    status = read_video_status()
    completed = status.get('completed_files') if isinstance(status.get('completed_files'), list) else []
    items = []
    for file_value in completed:
        if isinstance(file_value, dict):
            source_path = file_value.get('path') or file_value.get('file') or file_value.get('name') or ''
            title = file_value.get('title') or pathlib.Path(str(source_path)).name
        else:
            source_path = str(file_value or '')
            title = pathlib.Path(source_path).name or source_path
        if not source_path:
            continue
        items.append(make_video_edit_item(source_path, title=title, source='subtitle', status='pending_edit'))
    return items


def video_edit_sort_key(item):
    status_order = {
        'pending_edit': 0,
        'queued': 0,
        'editing': 1,
        'in_progress': 1,
        'done': 2,
        'completed': 2,
    }
    try:
        stamp = datetime.fromisoformat(str(task_sort_stamp(item))).timestamp()
    except Exception:
        stamp = 0
    return (status_order.get(item.get('status'), 1), -stamp)


def read_video_edit_queue():
    raw = load_video_edit_queue_raw()
    stored = raw.get('items') if isinstance(raw.get('items'), list) else []
    items = merge_video_edit_items(stored + subtitle_completed_items_from_status())
    items.sort(key=video_edit_sort_key)
    if items != stored:
        write_video_edit_queue(items)
    pending = len([item for item in items if item.get('status') in ('pending_edit', 'queued')])
    editing = len([item for item in items if item.get('status') in ('editing', 'in_progress')])
    done = len([item for item in items if item.get('status') in ('done', 'completed')])
    return {
        'ok': True,
        'updated_at': now_iso(),
        'items': items,
        'kpis': {
            'total': len(items),
            'pending': pending,
            'editing': editing,
            'done': done,
        },
    }


def save_video_edit_action(data):
    action = str(data.get('action') or 'add')
    state = read_video_edit_queue()
    items = state.get('items', [])

    if action in ('add', 'bulk_add'):
        additions = []
        if action == 'bulk_add':
            values = data.get('items') if isinstance(data.get('items'), list) else []
            for value in values:
                if isinstance(value, dict):
                    source_path = value.get('source_path') or value.get('path') or ''
                    title = value.get('title') or ''
                else:
                    source_path = str(value or '')
                    title = ''
                if source_path:
                    additions.append(make_video_edit_item(source_path, title=title, source=data.get('source') or 'subtitle'))
        else:
            source_path = str(data.get('source_path') or data.get('path') or '').strip()
            if not source_path:
                return {'ok': False, 'error': '영상 경로가 비어 있습니다.'}
            additions.append(make_video_edit_item(
                source_path,
                title=data.get('title') or '',
                source=data.get('source') or 'manual',
                note=data.get('note') or '',
            ))
        items = merge_video_edit_items(additions + items)

    elif action == 'update':
        item_id = str(data.get('id') or '')
        changed = False
        for item in items:
            if str(item.get('id')) != item_id:
                continue
            for key in ('title', 'status', 'note', 'cut_start', 'cut_end', 'layout', 'source_path'):
                if key in data:
                    item[key] = data.get(key)
            item['updated_at'] = now_iso()
            changed = True
            break
        if not changed:
            return {'ok': False, 'error': '대기열 항목을 찾지 못했습니다.'}

    elif action == 'delete':
        item_id = str(data.get('id') or '')
        items = [item for item in items if str(item.get('id')) != item_id]

    else:
        return {'ok': False, 'error': '지원하지 않는 영상 편집 작업입니다.'}

    items = merge_video_edit_items(items)
    items.sort(key=video_edit_sort_key)
    write_video_edit_queue(items)
    return read_video_edit_queue()


def parse_content_disposition(value):
    result = {}
    for part in str(value or '').split(';'):
        if '=' not in part:
            result[part.strip().lower()] = True
            continue
        key, raw = part.split('=', 1)
        result[key.strip().lower()] = raw.strip().strip('"')
    return result


def drain_multipart_part(handler, boundary):
    while True:
        line = handler.rfile.readline()
        if not line:
            return True
        if line.startswith(boundary):
            return line.rstrip().endswith(b'--')


def handle_video_upload(handler):
    content_type = handler.headers.get('Content-Type', '')
    match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
    if not match:
        return {'ok': False, 'error': 'multipart 업로드가 아닙니다.'}
    boundary = ('--' + (match.group(1) or match.group(2))).encode('utf-8')
    uploaded = []

    # Move to first boundary.
    while True:
        line = handler.rfile.readline()
        if not line:
            return {'ok': False, 'error': '업로드 본문을 읽지 못했습니다.'}
        if line.startswith(boundary):
            if line.rstrip().endswith(b'--'):
                return {'ok': False, 'error': '업로드된 파일이 없습니다.'}
            break

    done = False
    while not done:
        headers = {}
        while True:
            line = handler.rfile.readline()
            if not line:
                done = True
                break
            if line in (b'\r\n', b'\n'):
                break
            key, _, value = line.decode('utf-8', errors='ignore').partition(':')
            headers[key.strip().lower()] = value.strip()
        if done:
            break

        disposition = parse_content_disposition(headers.get('content-disposition', ''))
        filename = disposition.get('filename') or ''
        suffix = pathlib.Path(filename).suffix.lower()
        if not filename or suffix not in VIDEO_EXTENSIONS:
            done = drain_multipart_part(handler, boundary)
            continue

        dest = unique_upload_path(filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        bytes_written = 0
        previous = None
        with dest.open('wb') as f:
            while True:
                line = handler.rfile.readline()
                if not line:
                    done = True
                    break
                if line.startswith(boundary):
                    if previous is not None:
                        if previous.endswith(b'\r\n'):
                            previous = previous[:-2]
                        elif previous.endswith(b'\n'):
                            previous = previous[:-1]
                        f.write(previous)
                        bytes_written += len(previous)
                    done = line.rstrip().endswith(b'--')
                    break
                if previous is not None:
                    f.write(previous)
                    bytes_written += len(previous)
                previous = line

        uploaded.append({
            'name': dest.name,
            'path': str(dest),
            'size': bytes_written,
        })

    if not uploaded:
        return {'ok': False, 'error': '지원하는 영상 파일이 없습니다.'}

    purpose = (parse_qs(urlparse(handler.path).query).get('purpose') or ['edit'])[0]
    if purpose == 'encoding':
        return {
            'ok': True,
            'uploaded': uploaded,
            'upload_dir': str(pathlib.Path(uploaded[0]['path']).parent) if uploaded else '',
        }

    queue = save_video_edit_action({
        'action': 'bulk_add',
        'source': 'upload',
        'items': [{'source_path': item['path'], 'title': item['name']} for item in uploaded],
    })
    return {
        'ok': True,
        'uploaded': uploaded,
        'queue': queue,
    }


def normalize_category_id(name):
    slug = re.sub(r'[^0-9A-Za-z가-힣]+', '-', str(name).strip()).strip('-')
    return slug[:48] or 'uncategorized'


def read_categories():
    CATEGORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = read_json_file(CATEGORY_FILE, {'categories': []})
    categories = data.get('categories') if isinstance(data.get('categories'), list) else []
    by_id = {c.get('id'): c for c in categories if c.get('id')}
    changed = False

    for default in DEFAULT_CATEGORIES:
        existing = by_id.get(default['id'])
        if existing:
            for key, value in default.items():
                if key not in existing:
                    existing[key] = value
                    changed = True
        else:
            categories.append(default.copy())
            changed = True

    if changed or not CATEGORY_FILE.exists():
        data = {
            'updated_at': now_iso(),
            'categories': categories,
        }
        write_json_file(CATEGORY_FILE, data)

    return categories


def upsert_category(name, keywords=None, color='purple'):
    categories = read_categories()
    existing = next((c for c in categories if c.get('name') == name), None)
    if existing:
        return existing

    base_id = normalize_category_id(name)
    cat_id = base_id
    used_ids = {c.get('id') for c in categories}
    idx = 2
    while cat_id in used_ids:
        cat_id = f'{base_id}-{idx}'
        idx += 1

    category = {
        'id': cat_id,
        'name': name,
        'color': color,
        'keywords': keywords or [],
        'created_at': now_iso(),
        'autoCreated': True,
    }
    categories.append(category)
    write_json_file(CATEGORY_FILE, {
        'updated_at': now_iso(),
        'categories': categories,
    })
    return category


def choose_category(data):
    categories = read_categories()
    requested_id = data.get('categoryId') or data.get('category')
    requested_name = data.get('categoryName')

    if requested_id:
        match = next((c for c in categories if c.get('id') == requested_id), None)
        if match:
            return match

    if requested_name:
        match = next((c for c in categories if c.get('name') == requested_name), None)
        if match:
            return match

    text = f"{data.get('title', '')}\n{data.get('desc', '')}\n{data.get('type', '')}".lower()
    for category in categories:
        for keyword in category.get('keywords', []):
            if str(keyword).lower() in text:
                return category

    tokens = re.findall(r'[가-힣A-Za-z0-9]{2,}', text)
    stopwords = {
        '작업', '수정', '추가', '요청', '진행', '처리', '업데이트', '만들어', '해줘',
        '해서', '하도록', '부분', '기능', '내용', '관리', 'crata',
    }
    meaningful = [t for t in tokens if t not in stopwords]
    seed = meaningful[0] if meaningful else '기타'
    category_name = f'{seed[:12]} 업무'
    return upsert_category(category_name, [seed], 'purple')


def apply_task_category(task):
    category = choose_category(task)
    task['categoryId'] = category.get('id')
    task['categoryName'] = category.get('name')
    task['categoryColor'] = category.get('color', 'purple')
    return task


def read_calendar_state():
    data = read_json_file(CALENDAR_FILE, {'events': []})
    events = data.get('events') if isinstance(data.get('events'), list) else []
    events = [e for e in events if e.get('date')]
    events.sort(key=lambda e: f"{e.get('date', '')}T{e.get('time', '99:99')}")
    if not CALENDAR_FILE.exists():
        write_json_file(CALENDAR_FILE, {
            'updated_at': now_iso(),
            'events': events,
        })
    return {
        'updated_at': data.get('updated_at', ''),
        'events': events,
    }


def save_calendar_event(data):
    state = read_calendar_state()
    events = state.get('events', [])
    event_id = str(data.get('id') or datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3])
    title = (data.get('title') or '').strip() or '업무 일정'
    date = (data.get('date') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        date = datetime.now().strftime('%Y-%m-%d')
    time_value = (data.get('time') or '').strip()
    if time_value and not re.match(r'^\d{2}:\d{2}$', time_value):
        time_value = ''

    event = {
        'id': event_id,
        'title': title,
        'date': date,
        'time': time_value,
        'duration': str(data.get('duration') or '').strip(),
        'taskId': str(data.get('taskId') or '').strip(),
        'taskTitle': str(data.get('taskTitle') or '').strip(),
        'categoryName': str(data.get('categoryName') or '').strip(),
        'categoryColor': str(data.get('categoryColor') or 'blue').strip() or 'blue',
        'status': str(data.get('status') or 'planned').strip(),
        'note': str(data.get('note') or '').strip(),
        'created_at': data.get('created_at') or now_iso(),
        'updated_at': now_iso(),
    }

    replaced = False
    for idx, existing in enumerate(events):
        if str(existing.get('id')) == event_id:
            event['created_at'] = existing.get('created_at') or event['created_at']
            events[idx] = event
            replaced = True
            break
    if not replaced:
        events.append(event)

    events.sort(key=lambda e: f"{e.get('date', '')}T{e.get('time', '99:99')}")
    write_json_file(CALENDAR_FILE, {
        'updated_at': now_iso(),
        'events': events,
    })
    return {'ok': True, 'event': event, 'events': events}


def delete_calendar_event(data):
    event_id = str(data.get('id') or '')
    state = read_calendar_state()
    events = [e for e in state.get('events', []) if str(e.get('id')) != event_id]
    write_json_file(CALENDAR_FILE, {
        'updated_at': now_iso(),
        'events': events,
    })
    return {'ok': True, 'deleted': event_id, 'events': events}


def extract_first_json_object(text):
    if not text:
        return None
    fenced = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text, re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find('{')
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(candidate)):
        ch = candidate[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return candidate[start:idx + 1]
    return None


def local_calendar_draft(data):
    message = str(data.get('message') or '').strip()
    today = datetime.now().date()
    selected = str(data.get('selectedDate') or today.isoformat())
    try:
        base_date = datetime.strptime(selected, '%Y-%m-%d').date()
    except Exception:
        base_date = today

    target_date = base_date
    if re.search(r'오늘', message):
        target_date = today
    elif re.search(r'내일', message):
        target_date = today + timedelta(days=1)
    elif re.search(r'모레', message):
        target_date = today + timedelta(days=2)

    date_match = re.search(r'(\d{4})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})', message)
    if date_match:
        y, m, d = [int(x) for x in date_match.groups()]
        try:
            target_date = datetime(y, m, d).date()
        except Exception:
            pass
    else:
        md_match = re.search(r'(\d{1,2})\s*월\s*(\d{1,2})\s*일', message)
        if md_match:
            m, d = [int(x) for x in md_match.groups()]
            try:
                target_date = datetime(today.year, m, d).date()
            except Exception:
                pass

    time_value = ''
    colon_time = re.search(r'\b(\d{1,2}):(\d{2})\b', message)
    korean_time = re.search(r'(오전|오후|아침|저녁|밤)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?', message)
    if colon_time:
        hour = int(colon_time.group(1))
        minute = int(colon_time.group(2))
        time_value = f'{hour:02d}:{minute:02d}' if 0 <= hour < 24 and 0 <= minute < 60 else ''
    elif korean_time:
        marker = korean_time.group(1) or ''
        hour = int(korean_time.group(2))
        minute = int(korean_time.group(3) or 0)
        if marker in ('오후', '저녁', '밤') and hour < 12:
            hour += 12
        if marker == '오전' and hour == 12:
            hour = 0
        time_value = f'{hour:02d}:{minute:02d}' if 0 <= hour < 24 and 0 <= minute < 60 else ''

    duration = '60'
    hour_duration = re.search(r'(\d+)\s*시간', message)
    minute_duration = re.search(r'(\d+)\s*분', message)
    if hour_duration or minute_duration:
        minutes = 0
        if hour_duration:
            minutes += int(hour_duration.group(1)) * 60
        if minute_duration:
            minutes += int(minute_duration.group(1))
        duration = str(minutes or 60)

    cleaned_title = re.sub(
        r'오늘|내일|모레|오전|오후|아침|저녁|밤|\d{4}[.\-/년\s]+\d{1,2}[.\-/월\s]+\d{1,2}|\d{1,2}\s*월\s*\d{1,2}\s*일|\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?|\b\d{1,2}:\d{2}\b|\d+\s*시간|\d+\s*분|일정|캘린더|등록|저장|잡아|추가',
        ' ',
        message,
    )
    title = re.sub(r'\s+', ' ', cleaned_title).strip(' ,.-') or message[:40] or '업무 일정'
    category = choose_category({'title': title, 'desc': message})
    return {
        'title': title[:80],
        'date': target_date.isoformat(),
        'time': time_value,
        'duration': duration,
        'status': 'planned',
        'taskId': '',
        'taskTitle': '',
        'categoryName': category.get('name', '운영/기획'),
        'categoryColor': category.get('color', 'yellow'),
        'note': message,
        'confidence': 0.45,
        'questions': [] if time_value else ['시간이 명확하지 않아 날짜 중심 일정으로 잡았습니다.'],
    }


def normalize_calendar_draft(raw, data):
    raw = raw if isinstance(raw, dict) else {}
    fallback = local_calendar_draft(data)
    draft = {**fallback, **{k: v for k, v in raw.items() if v not in (None, '')}}

    draft['title'] = str(draft.get('title') or fallback['title']).strip()[:80] or '업무 일정'
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', str(draft.get('date') or '')):
        draft['date'] = fallback['date']
    if draft.get('time') and not re.match(r'^\d{2}:\d{2}$', str(draft.get('time'))):
        draft['time'] = fallback.get('time', '')
    draft['duration'] = str(draft.get('duration') or fallback.get('duration') or '60')
    draft['status'] = str(draft.get('status') or 'planned')
    draft['note'] = str(draft.get('note') or data.get('message') or '').strip()

    categories = read_categories()
    category_name = str(draft.get('categoryName') or '').strip()
    category = next((c for c in categories if c.get('name') == category_name), None)
    if not category:
        category = choose_category({'title': draft['title'], 'desc': draft['note']})
    draft['categoryName'] = category.get('name', fallback.get('categoryName', '운영/기획'))
    draft['categoryColor'] = category.get('color', fallback.get('categoryColor', 'yellow'))
    draft['taskId'] = str(draft.get('taskId') or '')
    draft['taskTitle'] = str(draft.get('taskTitle') or '')
    if not isinstance(draft.get('questions'), list):
        draft['questions'] = []
    try:
        draft['confidence'] = float(draft.get('confidence', 0.7))
    except Exception:
        draft['confidence'] = 0.7
    return draft


def build_calendar_parse_prompt(data):
    categories = read_categories()
    tasks = data.get('tasks') if isinstance(data.get('tasks'), list) else []
    compact_tasks = [
        {
            'id': str(t.get('id') or '')[:80],
            'title': str(t.get('title') or '')[:120],
            'categoryName': str(t.get('categoryName') or '')[:80],
        }
        for t in tasks[:30]
    ]
    previous = data.get('previousDraft') if isinstance(data.get('previousDraft'), dict) else {}
    context = {
        'today': datetime.now().strftime('%Y-%m-%d'),
        'selectedDate': data.get('selectedDate') or datetime.now().strftime('%Y-%m-%d'),
        'categories': [{'name': c.get('name'), 'color': c.get('color')} for c in categories],
        'tasks': compact_tasks,
        'previousDraft': previous,
        'message': data.get('message') or '',
    }
    return f"""CRATA 대시보드 캘린더 일정 파서입니다.

규칙:
- 파일을 읽거나 수정하지 마세요. 도구 실행, PLAUD 처리, 작업 수행도 하지 마세요.
- 사용자의 자연어 요청을 일정 초안 JSON 하나로만 변환하세요.
- 상대 날짜는 context.today 기준으로 계산하세요. 날짜가 없으면 context.selectedDate를 쓰세요.
- 연결할 기존 작업이 명확하면 tasks의 id/title을 taskId/taskTitle에 넣고, 아니면 비워두세요.
- categoryName은 categories 중 하나를 고르세요. 없으면 가장 가까운 카테고리를 고르세요.
- 시간은 24시간 HH:MM, 날짜는 YYYY-MM-DD, duration은 분 단위 문자열입니다.
- 사용자가 이전 초안을 수정하는 말이면 previousDraft를 반영해서 새 초안을 반환하세요.
- 설명은 쓰지 말고 JSON만 출력하세요.

반환 형식:
{{
  "title": "일정 제목",
  "date": "YYYY-MM-DD",
  "time": "HH:MM 또는 빈 문자열",
  "duration": "60",
  "status": "planned",
  "taskId": "",
  "taskTitle": "",
  "categoryName": "카테고리명",
  "categoryColor": "blue|green|yellow|red|purple",
  "note": "사용자 요청 원문 또는 보완 메모",
  "confidence": 0.0,
  "questions": []
}}

context:
{json.dumps(context, ensure_ascii=False, indent=2)}
"""


def run_calendar_cli(prompt, runner):
    env = os.environ.copy()
    env['NO_COLOR'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    started = time.time()
    runner = runner if runner in ('claude', 'codex') else 'claude'

    if runner == 'codex':
        result_file = pathlib.Path(tempfile.gettempdir()) / f'crata_calendar_parse_{int(time.time() * 1000)}.txt'
        args = [
            resolve_codex_command(),
            'exec',
            '--cd',
            str(ROOT),
            '--skip-git-repo-check',
            '--sandbox',
            'danger-full-access',
            '--color',
            'never',
            '--output-last-message',
            str(result_file),
            '-',
        ]
        proc = subprocess.run(
            args,
            input=prompt,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=70,
            env=env,
        )
        output = proc.stdout or ''
        if result_file.exists():
            try:
                file_output = result_file.read_text(encoding='utf-8')
                if file_output.strip():
                    output = file_output
                result_file.unlink()
            except Exception:
                pass
        return {
            'runner': runner,
            'ok': proc.returncode == 0,
            'output': output.strip(),
            'elapsed': round(time.time() - started, 1),
            'error': '' if proc.returncode == 0 else f'Codex CLI 종료 코드: {proc.returncode}',
        }

    args = [
        resolve_claude_command(),
        '-p',
        prompt,
        '--output-format',
        'stream-json',
        '--include-partial-messages',
        '--effort',
        'medium',
        '--mcp-config',
        '{"mcpServers":{}}',
        '--strict-mcp-config',
    ]
    proc = subprocess.run(
        args,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=70,
        env=env,
    )
    parts = []
    for line in (proc.stdout or '').splitlines():
        try:
            text, replace = extract_stream_text(json.loads(line))
            if text:
                parts = [text] if replace else parts + [text]
        except Exception:
            if line.strip():
                parts.append(line.strip())
    return {
        'runner': runner,
        'ok': proc.returncode == 0,
        'output': ''.join(parts).strip() or (proc.stdout or '').strip(),
        'elapsed': round(time.time() - started, 1),
        'error': '' if proc.returncode == 0 else f'Claude CLI 종료 코드: {proc.returncode}',
    }


def parse_calendar_request(data):
    message = str(data.get('message') or '').strip()
    if not message:
        return {'ok': False, 'error': '일정 요청 내용이 비어 있습니다.'}

    prompt = build_calendar_parse_prompt(data)
    primary = data.get('runnerPreference') if data.get('runnerPreference') in ('claude', 'codex') else 'claude'
    runners = [primary, 'codex' if primary == 'claude' else 'claude']
    attempts = []

    for runner in runners:
        try:
            result = run_calendar_cli(prompt, runner)
        except Exception as exc:
            result = {
                'runner': runner,
                'ok': False,
                'output': '',
                'elapsed': 0,
                'error': f'{runner} CLI 실행 실패: {exc}',
            }
        attempts.append(result)
        json_text = extract_first_json_object(result.get('output') or '')
        if result.get('ok') and json_text:
            try:
                draft = normalize_calendar_draft(json.loads(json_text), data)
                return {
                    'ok': True,
                    'draft': draft,
                    'runner': runner,
                    'source': 'cli',
                    'elapsed': result.get('elapsed', 0),
                    'attempts': attempts,
                    'raw': result.get('output', '')[:2000],
                }
            except Exception as exc:
                result['error'] = f'JSON 파싱 실패: {exc}'

    draft = normalize_calendar_draft(local_calendar_draft(data), data)
    return {
        'ok': True,
        'draft': draft,
        'runner': attempts[-1]['runner'] if attempts else 'local',
        'source': 'local-fallback',
        'elapsed': sum(float(a.get('elapsed') or 0) for a in attempts),
        'attempts': attempts,
        'warning': 'CLI 일정 파싱에 실패해 로컬 규칙으로 초안을 만들었습니다.',
    }


def append_task_log(task, sender, msg):
    logs = task.setdefault('collabLogs', [])
    logs.append({
        'sender': sender,
        'icon': '⚙' if sender == '시스템' else '◈',
        'msg': msg,
        'time': clock_time(),
    })
    if len(logs) > 40:
        task['collabLogs'] = logs[-40:]


def update_task_file(fpath, mutator):
    with TASK_LOCK:
        task = read_json_file(fpath, {})
        mutator(task)
        task['updated_at'] = now_iso()
        write_json_file(fpath, task)
        return task


def normalize_task_identity_text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().lower()


def task_identity_key(task):
    task_type = normalize_task_identity_text(task.get('type'))
    meeting_id = normalize_task_identity_text(task.get('meetingId') or task.get('meeting_id'))
    title = normalize_task_identity_text(task.get('title'))
    desc = normalize_task_identity_text(task.get('desc'))
    category = normalize_task_identity_text(task.get('categoryId') or task.get('categoryName'))

    if task_type:
        suffix = meeting_id or title or desc[:80]
        return f'type:{task_type}:{suffix}'
    return f'manual:{category}:{title or desc[:80] or task.get("id", "")}'


def task_sort_stamp(task):
    return (
        task.get('updated_at')
        or task.get('completed_at')
        or task.get('fallback_started_at')
        or task.get('started_at')
        or task.get('created_at')
        or task.get('date')
        or ''
    )


def task_process_alive(task):
    pid = task.get('pid')
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def terminate_process_tree(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False, 'PID가 없습니다.'
    if pid <= 0:
        return False, 'PID가 올바르지 않습니다.'

    try:
        if os.name == 'nt':
            proc = subprocess.run(
                ['taskkill', '/PID', str(pid), '/T', '/F'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=8,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            if proc.returncode == 0:
                return True, f'PID {pid} 및 하위 프로세스를 종료했습니다.'
            return False, f'taskkill 종료 코드 {proc.returncode}'
        os.kill(pid, 15)
        return True, f'PID {pid} 종료 신호를 보냈습니다.'
    except ProcessLookupError:
        return True, f'PID {pid}는 이미 종료되었습니다.'
    except Exception as exc:
        return False, str(exc)


def worker_process_kwargs():
    if os.name != 'nt':
        return {}
    flags = (
        getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    )
    return {'creationflags': flags} if flags else {}


def task_is_cancelled(fpath):
    try:
        task = read_json_file(fpath, {})
        return task.get('status') == 'cancelled'
    except Exception:
        return False


def find_video_task_files(task_id=''):
    inbox_dir = ROOT / '처리관리' / 'inbox'
    if not inbox_dir.exists():
        return []
    matches = []
    for fpath in inbox_dir.glob('task_*.json'):
        task = read_json_file(fpath, {})
        if task.get('type') != 'video_encoding':
            continue
        if task_id and str(task.get('id') or '') != str(task_id):
            continue
        matches.append((fpath, task))
    matches.sort(key=lambda item: task_sort_stamp(item[1]), reverse=True)
    return matches


def compact_video_status_snapshot(status):
    snapshot = {}
    for key in VIDEO_STATUS_SNAPSHOT_KEYS:
        value = status.get(key)
        if value not in (None, '', [], {}):
            snapshot[key] = value
    if snapshot.get('process_steps') is None:
        snapshot['process_steps'] = VIDEO_PROCESS_STEPS
    return snapshot


def video_task_live_status(task, current_status=None):
    if not current_status:
        return {}
    task_active = task.get('status') in ('pending', 'active') and task_process_alive(task)
    if task_active:
        return compact_video_status_snapshot(current_status)
    runner_task = current_status.get('runner_task') if isinstance(current_status.get('runner_task'), dict) else {}
    if runner_task.get('id') and str(runner_task.get('id')) == str(task.get('id') or ''):
        return compact_video_status_snapshot(current_status)
    return {}


def video_task_process_snapshot(task, current_status=None):
    live_status = video_task_live_status(task, current_status)
    if live_status:
        return enrich_video_process_results(dict(live_status))
    stored = task.get('videoStatusSnapshot')
    snapshot = dict(stored) if isinstance(stored, dict) else {}
    if not snapshot.get('source_path') and task.get('videoSourcePath'):
        snapshot['source_path'] = task.get('videoSourcePath')
    if not snapshot.get('workspace_path'):
        workspace_path = find_video_task_workspace_path(task)
        if workspace_path:
            snapshot['workspace_path'] = workspace_path
    if not snapshot:
        return {}
    return enrich_video_process_results(snapshot)


def record_video_task_status_snapshot(fpath):
    status = compact_video_status_snapshot(read_video_status())
    if not status:
        return {}

    def mutate(t):
        t['videoStatusSnapshot'] = status
        t['videoStatusSnapshotAt'] = now_iso()
        if status.get('status') == 'waiting_preview_review':
            t['status'] = 'waiting_review'
            t['statusLbl'] = '확인 대기'
            try:
                t['progress'] = max(int(t.get('progress') or 0), int(status.get('progress') or 0))
            except (TypeError, ValueError):
                t['progress'] = int(t.get('progress') or 0)
            append_task_log(t, '시스템', '미리보기 검수 대기 상태로 멈췄습니다. 확인 후 승인하면 후속 인코딩을 진행하세요.')

    update_task_file(fpath, mutate)
    return status


def video_queue_payload(limit=20, current_status=None):
    items = []
    for fpath, task in find_video_task_files():
        try:
            progress = int(task.get('progress') or 0)
        except (TypeError, ValueError):
            progress = 0
        active = task.get('status') in ('pending', 'active') and task_process_alive(task)
        snapshot = video_task_process_snapshot(task, current_status)
        item = {
            'id': task.get('id') or pathlib.Path(fpath).stem.removeprefix('task_'),
            'title': task.get('title') or pathlib.Path(fpath).name,
            'status': task.get('status') or '',
            'status_label': task.get('statusLbl') or '',
            'progress': max(0, min(100, progress)),
            'active': bool(active),
            'pid': task.get('pid') or '',
            'source_path': task.get('videoSourcePath') or '',
            'workspace_root': task.get('videoWorkspaceRoot') or '',
            'file_count': task.get('videoFileCount') or 0,
            'steps': task.get('videoSteps') if isinstance(task.get('videoSteps'), list) else [],
            'created_at': task.get('created_at') or '',
            'queued_at': task.get('queued_at') or '',
            'started_at': task.get('started_at') or task.get('fallback_started_at') or '',
            'updated_at': task.get('updated_at') or '',
            'completed_at': task.get('completed_at') or '',
            'cancelled_at': task.get('cancelled_at') or '',
            'error': task.get('error') or '',
            'collab_logs': task.get('collabLogs') if isinstance(task.get('collabLogs'), list) else [],
        }
        for key in (
            'current_process', 'current_process_label', 'process_steps', 'process_status',
            'process_results', 'artifacts', 'message', 'preview_file', 'completed_files',
            'workspace_path', 'source_path', 'current_file',
        ):
            if snapshot.get(key) not in (None, '', [], {}):
                item[key] = snapshot.get(key)
        if snapshot.get('progress') is not None and active:
            try:
                item['progress'] = max(item['progress'], int(float(snapshot.get('progress') or 0)))
            except (TypeError, ValueError):
                pass
        items.append(item)
    return items[:limit]


def video_task_is_running(task):
    status = task.get('status')
    if status not in ('pending', 'active'):
        return False
    if status == 'queued':
        return False
    return task_process_alive(task)


def should_queue_video_task(task, current_fpath=None):
    if task.get('type') != 'video_encoding':
        return False
    current_key = str(current_fpath or '').lower()
    for fpath, existing in find_video_task_files():
        if current_key and str(fpath).lower() == current_key:
            continue
        if video_task_is_running(existing):
            return True
    return False


def queued_video_task_files():
    items = []
    for fpath, task in find_video_task_files():
        if task.get('status') in ('queued', 'pending') and not task_process_alive(task):
            items.append((fpath, task))
    items.sort(key=lambda item: item[1].get('created_at') or item[1].get('queued_at') or task_sort_stamp(item[1]))
    return items


def auto_start_next_video_task():
    if should_queue_video_task({'type': 'video_encoding'}):
        return None
    queued = queued_video_task_files()
    if not queued:
        return None

    fpath, task = queued[0]
    runner_preference = task.get('runnerPreference') if task.get('runnerPreference') in ('claude', 'codex') else 'codex'
    update_task_file(fpath, lambda t: (
        t.update({
            'status': 'pending',
            'statusLbl': '대기 중',
            'progress': max(0, int(t.get('progress') or 0)),
            'dequeued_at': now_iso(),
        }),
        append_task_log(t, '시스템', '이전 영상 작업이 끝나 대기열에서 자동 실행합니다.')
    ))
    start_task_worker(fpath, runner_preference)
    return fpath


def continue_video_queue_after_task(fpath):
    task = read_json_file(fpath, {})
    if task.get('type') != 'video_encoding':
        return None
    snapshot = record_video_task_status_snapshot(fpath)
    return auto_start_next_video_task()


def cancel_video_encoding_task(data=None):
    data = data or {}
    task_id = str(data.get('taskId') or data.get('task_id') or '').strip()
    candidates = find_video_task_files(task_id)
    if not candidates:
        return {'ok': False, 'error': '중지할 영상 작업을 찾지 못했습니다.'}

    selected = []
    for fpath, task in candidates:
        activeish = task.get('status') in ('pending', 'active') or task_process_alive(task)
        if task_id or activeish:
            selected.append((fpath, task))
        if selected and not task_id:
            break

    if not selected:
        return {'ok': False, 'error': '진행 중인 영상 작업이 없습니다.'}

    stopped = []
    errors = []
    for fpath, task in selected:
        pid = task.get('pid')
        killed = False
        kill_message = '실행 중인 PID 없음'
        if pid:
            killed, kill_message = terminate_process_tree(pid)
            if not killed:
                errors.append({'task': task.get('id'), 'pid': pid, 'error': kill_message})

        cancelled_task = update_task_file(fpath, lambda t, killed=killed, kill_message=kill_message: (
            t.update({
                'status': 'cancelled',
                'statusLbl': '중지됨',
                'progress': int(t.get('progress') or 0),
                'cancelled_at': now_iso(),
                'cancel_reason': data.get('reason') or '사용자 요청',
                'result': (t.get('result') or '') + '\n\n[시스템] 사용자 요청으로 영상 작업을 중지했습니다.',
                'runner': {**(t.get('runner') or {}), 'cancelled': True},
            }),
            append_task_log(t, '시스템', f'사용자 요청으로 작업을 중지했습니다. {kill_message}')
        ))
        stopped.append({'id': cancelled_task.get('id'), 'title': cancelled_task.get('title'), 'pid': pid, 'killed': killed, 'message': kill_message})

    video_status = read_video_status()
    current_process = infer_current_video_process(video_status) or video_status.get('current_process') or ''
    if current_process:
        process_status = video_status.get('process_status') if isinstance(video_status.get('process_status'), dict) else {}
        current_info = process_status.get(current_process) if isinstance(process_status.get(current_process), dict) else {}
        process_status[current_process] = {
            **current_info,
            'status': 'cancelled',
            'message': '사용자 요청으로 중지됨',
        }
        video_status['process_status'] = process_status
    video_status.update({
        'status': 'cancelled',
        'status_label': '중지됨',
        'message': '사용자 요청으로 영상 작업을 중지했습니다.',
        'cancelled_at': now_iso(),
        'updated_at': now_iso(),
    })
    write_json_file(VIDEO_STATUS_FILE, video_status)
    next_task = auto_start_next_video_task()

    return {'ok': not errors, 'stopped': stopped, 'errors': errors, 'nextTask': str(next_task) if next_task else '', 'videoStatus': video_status_payload()}


def find_existing_task_file(task):
    inbox_dir = ROOT / '처리관리' / 'inbox'
    target_key = task_identity_key(task)
    matches = []
    if not target_key or not inbox_dir.exists():
        return None, None

    for fpath in inbox_dir.glob('task_*.json'):
        existing = read_json_file(fpath, {})
        if task_identity_key(existing) == target_key:
            matches.append((fpath, existing))

    if not matches:
        return None, None

    matches.sort(key=lambda item: task_sort_stamp(item[1]), reverse=True)
    return matches[0]


def merge_duplicate_task_list(tasks):
    grouped = {}
    duplicate_files = {}

    for task in tasks:
        key = task_identity_key(task)
        current = grouped.get(key)
        duplicate_files.setdefault(key, []).append(task.get('_file'))
        if not current or task_sort_stamp(task) >= task_sort_stamp(current):
            grouped[key] = task

    merged = []
    for key, task in grouped.items():
        files = [f for f in duplicate_files.get(key, []) if f]
        if len(files) > 1:
            task['_duplicateCount'] = len(files)
            task['_duplicateFiles'] = files
        merged.append(task)

    merged.sort(key=task_sort_stamp, reverse=True)
    return merged


def build_claude_prompt(task):
    desc = task.get('desc') or task.get('title') or ''
    agent_name = task.get('agentName') or task.get('agent') or '자동'
    priority = task.get('priorityLbl') or task.get('priority') or '보통'

    return f"""대시보드에서 전달된 CRATA 에이전트 작업입니다.

담당: {agent_name}
우선순위: {priority}
작업 내용:
{desc}

작업 지침:
- 현재 작업 디렉토리의 AGENTS.md / CLAUDE.md 지침을 우선해서 따르세요.
- 이 호출은 대시보드 작업 실행용입니다. 사용자가 녹음/PLAUD 처리를 명시적으로 요청한 작업이 아니라면 PLAUD 새 녹음 처리 루틴은 실행하지 마세요.
- 문구·지식 파일 수정은 프로젝트 규칙대로 제안 → 사용자 확인 → 반영 순서를 지키세요.
- 사용자가 명시적으로 '승인', '반영', '적용'을 요청하지 않았다면 파일을 바로 수정하지 말고 제안, 근거, 확인 질문을 출력하세요.
- 결과는 대시보드에 표시될 수 있도록 한국어로 간결하게 정리하세요.
"""


def task_requests_plaud(task):
    if str(task.get('type') or '').startswith('plaud_'):
        return True

    text = f"{task.get('title', '')}\n{task.get('desc', '')}"
    if re.search(r'금지|하지\s*말|하지\s*마|제외|skip|스킵', text, re.IGNORECASE):
        return False
    return bool(re.search(
        r'plaud|recording|transcript|(녹음|전사).{0,24}(처리|읽|확인|조회|요약|분석|가져)',
        text,
        re.IGNORECASE,
    ))


def extract_stream_text(event):
    """Return (text, replace_existing). Ignore thinking/tool JSON noise."""
    if event.get('type') == 'result':
        return event.get('result') or '', True

    if event.get('type') != 'stream_event':
        return '', False

    inner = event.get('event') or {}
    if inner.get('type') != 'content_block_delta':
        return '', False

    delta = inner.get('delta') or {}
    if delta.get('type') == 'text_delta':
        return delta.get('text') or '', False

    return '', False


def run_codex_task(fpath, claude_error='', claude_output='', fallback=False):
    if task_is_cancelled(fpath):
        return
    codex_cmd = resolve_codex_command()
    started = time.time()
    output_parts = []
    result_file = pathlib.Path(tempfile.gettempdir()) / f'crata_codex_result_{pathlib.Path(fpath).stem}.txt'
    if result_file.exists():
        try:
            result_file.unlink()
        except Exception:
            pass

    task = update_task_file(fpath, lambda t: (
        t.update({
            'status': 'active',
            'statusLbl': 'Codex 재시도' if fallback else '진행 중',
            'progress': max(int(t.get('progress') or 0), 55 if fallback else 10),
            **({'fallback_started_at': now_iso()} if fallback else {'started_at': now_iso()}),
            'runner': {
                'active': 'codex',
                'primary': 'claude' if fallback else 'codex',
                'fallback': 'codex' if fallback else None,
                'command': codex_cmd,
                'mode': 'codex exec --output-last-message',
                'claude_error': claude_error,
            },
            'result': 'Claude 실패 후 Codex가 작업을 이어받아 처리 중입니다.' if fallback else '',
            'error': '',
        }),
        append_task_log(t, '시스템', 'Claude 실행 실패로 Codex CLI 재시도를 시작했습니다.' if fallback else 'Codex CLI 실행을 시작했습니다.')
    ))

    prompt = build_claude_prompt(task)
    if fallback:
        prompt = prompt.replace(
            '대시보드에서 전달된 CRATA 에이전트 작업입니다.',
            '대시보드에서 전달된 CRATA 에이전트 작업입니다. Claude 실패 후 Codex가 이어받아 처리합니다.',
            1,
        )

    args = [
        codex_cmd,
        'exec',
        '--cd',
        str(ROOT),
        '--skip-git-repo-check',
        '--sandbox',
        'danger-full-access',
        '--color',
        'never',
        '--output-last-message',
        str(result_file),
        '-',
    ]

    env = os.environ.copy()
    env['NO_COLOR'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=env,
            **worker_process_kwargs(),
        )
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception as exc:
        update_task_file(fpath, lambda t: (
            t.update({
                'status': 'error',
                'statusLbl': '실패',
                'progress': 100,
                'result': claude_output,
                'error': f'{claude_error}\nCodex CLI 실행 실패: {exc}'.strip() if fallback else f'Codex CLI 실행 실패: {exc}',
                'completed_at': now_iso(),
            }),
            append_task_log(t, '시스템', 'Codex CLI 실행에 실패했습니다.')
        ))
        continue_video_queue_after_task(fpath)
        return

    update_task_file(fpath, lambda t: (
        t.update({'pid': proc.pid, 'progress': max(int(t.get('progress') or 0), 65)}),
        append_task_log(t, '시스템', f'Codex 프로세스 PID {proc.pid}로 실행 중입니다.')
    ))
    if task_is_cancelled(fpath):
        terminate_process_tree(proc.pid)
        return

    last_flush = 0
    for line in proc.stdout or []:
        text = line.rstrip()
        if text:
            output_parts.append(text)
            joined = '\n'.join(output_parts)[-MAX_OUTPUT_CHARS:]
            output_parts = [joined]

        if time.time() - last_flush > 1.0:
            elapsed = max(time.time() - started, 1)
            update_task_file(fpath, lambda t, joined='\n'.join(output_parts), elapsed=elapsed: (
                t.update({
                    'result': 'Claude 실패 후 Codex가 작업을 이어받아 처리 중입니다.' if fallback else 'Codex가 작업을 처리 중입니다.',
                    'progress': max(int(t.get('progress') or 0), 75),
                    'collabStats': {
                        'calls': 2 if fallback else 1,
                        'tokens': 'codex',
                        'cost': '—',
                        'speed': f'{elapsed:.1f}s',
                    },
                })
            ))
            last_flush = time.time()

    return_code = proc.wait()
    elapsed = max(time.time() - started, 0.1)
    stdout_output = '\n'.join(output_parts).strip()
    final_output = stdout_output
    if result_file.exists():
        try:
            file_output = result_file.read_text(encoding='utf-8').strip()
            if file_output:
                final_output = file_output
            result_file.unlink()
        except Exception:
            pass

    if task_is_cancelled(fpath):
        return

    if return_code == 0:
        update_task_file(fpath, lambda t: (
            t.update({
                'status': 'done',
                'statusLbl': '완료됨',
                'progress': 100,
                'result': final_output or '(Codex가 빈 응답을 반환했습니다.)',
                'completed_at': now_iso(),
                'collabStats': {
                    'calls': 2 if fallback else 1,
                    'tokens': 'codex',
                    'cost': '—',
                    'speed': f'{elapsed:.1f}s',
                },
            }),
            enrich_task_result(t, final_output or '(Codex가 빈 응답을 반환했습니다.)'),
            append_task_log(t, '시스템', 'Codex CLI 실행이 완료되었습니다.')
        ))
    else:
        combined_error = f'{claude_error}\nCodex CLI 종료 코드: {return_code}'.strip() if fallback else f'Codex CLI 종료 코드: {return_code}'
        update_task_file(fpath, lambda t: (
            t.update({
                'status': 'error',
                'statusLbl': '실패',
                'progress': 100,
                'result': final_output or stdout_output or claude_output,
                'error': combined_error,
                'completed_at': now_iso(),
                'collabStats': {
                    'calls': 2 if fallback else 1,
                    'tokens': 'codex',
                    'cost': '—',
                    'speed': f'{elapsed:.1f}s',
                },
            }),
            append_task_log(t, '시스템', f'Codex CLI가 종료 코드 {return_code}로 종료되었습니다.')
        ))
    continue_video_queue_after_task(fpath)


def run_codex_fallback_task(fpath, claude_error='', claude_output=''):
    run_codex_task(fpath, claude_error=claude_error, claude_output=claude_output, fallback=True)


def run_claude_task(fpath):
    if task_is_cancelled(fpath):
        return
    claude_cmd = resolve_claude_command()
    started = time.time()
    output_parts = []
    last_flush = 0

    task = update_task_file(fpath, lambda t: (
        t.update({
            'status': 'active',
            'statusLbl': '진행 중',
            'progress': max(int(t.get('progress') or 0), 10),
            'started_at': now_iso(),
            'runner': {
                'active': 'claude',
                'primary': 'claude',
                'fallback': 'codex',
                'command': claude_cmd,
                'mode': 'claude -p --output-format stream-json --verbose',
            },
            'result': '',
            'error': '',
        }),
        append_task_log(t, '시스템', 'Claude CLI 실행을 시작했습니다.')
    ))

    prompt = build_claude_prompt(task)
    args = [
        claude_cmd,
        '-p',
        prompt,
        '--output-format',
        'stream-json',
        '--include-partial-messages',
        '--verbose',
        '--effort',
        'medium',
        '--append-system-prompt',
        'This is a dashboard worker invocation. Do not run PLAUD recording intake unless the user task explicitly asks for PLAUD or recording processing.',
    ]

    if task_requests_plaud(task):
        args.extend([
            '--mcp-config',
            plaud_mcp_config_json(),
            '--strict-mcp-config',
        ])
    else:
        args.extend([
            '--mcp-config',
            '{"mcpServers":{}}',
            '--strict-mcp-config',
        ])

    env = os.environ.copy()
    env['NO_COLOR'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=env,
            **worker_process_kwargs(),
        )
    except Exception as exc:
        error_msg = f'Claude CLI 실행 실패: {exc}'
        update_task_file(fpath, lambda t: (
            t.update({
                'status': 'active',
                'statusLbl': 'Codex 재시도',
                'progress': 50,
                'error': error_msg,
            }),
            append_task_log(t, '시스템', 'Claude CLI 실행에 실패했습니다.')
        ))
        run_codex_fallback_task(fpath, error_msg, '')
        return

    update_task_file(fpath, lambda t: (
        t.update({'pid': proc.pid, 'progress': 25}),
        append_task_log(t, '시스템', f'프로세스 PID {proc.pid}로 실행 중입니다.')
    ))
    if task_is_cancelled(fpath):
        terminate_process_tree(proc.pid)
        return

    for line in proc.stdout or []:
        line = line.strip()
        if not line:
            continue

        text = ''
        replace_output = False
        try:
            event = json.loads(line)
            text, replace_output = extract_stream_text(event)
        except Exception:
            text = line

        if text:
            if replace_output:
                output_parts = [text]
            else:
                output_parts.append(text)
            joined = ''.join(output_parts)[-MAX_OUTPUT_CHARS:]
            output_parts = [joined]

        if time.time() - last_flush > 1.0:
            elapsed = max(time.time() - started, 1)
            progress = 55 if elapsed < 20 else 75
            update_task_file(fpath, lambda t, joined=''.join(output_parts), progress=progress: (
                t.update({
                    'result': joined,
                    'progress': max(int(t.get('progress') or 0), progress),
                    'collabStats': {
                        'calls': 1,
                        'tokens': 'stream',
                        'cost': '—',
                        'speed': f'{elapsed:.1f}s',
                    },
                })
            ))
            last_flush = time.time()

    return_code = proc.wait()
    elapsed = max(time.time() - started, 0.1)
    final_output = ''.join(output_parts).strip()

    if task_is_cancelled(fpath):
        return

    if return_code == 0:
        update_task_file(fpath, lambda t: (
            t.update({
                'status': 'done',
                'statusLbl': '완료됨',
                'progress': 100,
                'result': final_output or '(Claude가 빈 응답을 반환했습니다.)',
                'completed_at': now_iso(),
                'collabStats': {
                    'calls': 1,
                    'tokens': 'stream',
                    'cost': '—',
                    'speed': f'{elapsed:.1f}s',
                },
            }),
            enrich_task_result(t, final_output or '(Claude가 빈 응답을 반환했습니다.)'),
            append_task_log(t, '시스템', 'Claude CLI 실행이 완료되었습니다.')
        ))
        continue_video_queue_after_task(fpath)
    else:
        claude_error = f'Claude CLI 종료 코드: {return_code}'
        update_task_file(fpath, lambda t: (
            t.update({
                'status': 'active',
                'statusLbl': 'Codex 재시도',
                'progress': max(int(t.get('progress') or 0), 50),
                'result': final_output,
                'error': claude_error,
            }),
            append_task_log(t, '시스템', f'Claude CLI가 종료 코드 {return_code}로 종료되었습니다.')
        ))
        run_codex_fallback_task(fpath, claude_error, final_output)
        return


def start_claude_worker(fpath):
    thread = threading.Thread(target=run_claude_task, args=(pathlib.Path(fpath),), daemon=True)
    thread.start()
    return thread


def start_codex_worker(fpath):
    thread = threading.Thread(target=run_codex_task, args=(pathlib.Path(fpath),), daemon=True)
    thread.start()
    return thread


def start_task_worker(fpath, runner_preference):
    if runner_preference == 'codex':
        return start_codex_worker(fpath)
    return start_claude_worker(fpath)


def save_task_request(data, start=True):
    inbox_dir = ROOT / '처리관리' / 'inbox'
    inbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
    runner_preference = data.get('runnerPreference') if data.get('runnerPreference') in ('claude', 'codex') else 'claude'
    desc = data.get('desc') or data.get('title') or ''
    title = data.get('title') or (desc[:32] + ('...' if len(desc) > 32 else '')) or '새 작업'

    task = {
        'id': ts,
        'created_at': now_iso(),
        'date': datetime.now().strftime('%Y-%m-%d'),
        'title': title,
        'desc': desc,
        'status': 'pending',
        'statusLbl': '대기 중',
        'priority': 'normal',
        'priorityLbl': '보통',
        'agent': 'plan',
        'agentName': '기획담당',
        'progress': 0,
        **data,
        'runnerPreference': runner_preference,
    }
    apply_task_category(task)
    if not task.get('collabLogs'):
        append_task_log(task, '시스템', f"{task.get('categoryName', '미분류')} 카테고리로 작업이 등록되었습니다.")

    existing_fpath, existing_task = find_existing_task_file(task)
    if existing_fpath:
        task['id'] = existing_task.get('id') or existing_fpath.stem.removeprefix('task_')
        task['created_at'] = existing_task.get('created_at') or task['created_at']
        task['requeued_at'] = now_iso()
        task['collabLogs'] = existing_task.get('collabLogs', [])[-30:]
        append_task_log(task, '시스템', '동일 작업이 다시 요청되어 기존 작업 카드의 상태를 갱신했습니다.')

        active_existing = existing_task.get('status') in ('pending', 'active') and task_process_alive(existing_task)
        if active_existing:
            refreshed = update_task_file(existing_fpath, lambda t: (
                t.update({
                    'runnerPreference': runner_preference,
                    'categoryId': task.get('categoryId'),
                    'categoryName': task.get('categoryName'),
                    'categoryColor': task.get('categoryColor'),
                }),
                append_task_log(t, '시스템', '이미 실행 중인 동일 작업이 있어 새 카드를 만들지 않았습니다.')
            ))
            print(f'  → 기존 작업 재사용: {existing_fpath.name}')
            return refreshed, existing_fpath

        write_json_file(existing_fpath, task)
        fpath = existing_fpath
        print(f'  → 기존 작업 갱신: {fpath.name}')
    else:
        fpath = inbox_dir / f'task_{ts}.json'
        write_json_file(fpath, task)
        print(f'  → 작업 저장: {fpath.name}')

    if start:
        if task.get('type') == 'video_encoding' and should_queue_video_task(task, current_fpath=fpath):
            queued_task = update_task_file(fpath, lambda t: (
                t.update({
                    'status': 'queued',
                    'statusLbl': '대기열',
                    'progress': 0,
                    'queued_at': now_iso(),
                }),
                append_task_log(t, '시스템', '진행 중인 영상 작업이 있어 대기열에 추가했습니다. 현재 작업이 끝나면 자동으로 시작합니다.')
            ))
            return queued_task, fpath
        start_task_worker(fpath, runner_preference)
    return task, fpath


def read_processed_recordings():
    processed = {}
    data = read_json_file(PROCESSED_FILE, {'processed': []})
    for item in data.get('processed', []):
        if item.get('id'):
            processed[item['id']] = item
    return processed


def read_meetings_state():
    cache = read_json_file(MEETINGS_FILE, {'meetings': [], 'updated_at': ''})
    meetings = cache.get('meetings') if isinstance(cache.get('meetings'), list) else []
    processed = read_processed_recordings()
    by_id = {m.get('id'): dict(m) for m in meetings if m.get('id')}

    for meeting_id, item in processed.items():
        by_id.setdefault(meeting_id, {
            'id': meeting_id,
            'title': item.get('title', meeting_id),
            'recorded_at': item.get('recorded_at', ''),
        })

    normalized = []
    for meeting_id, item in by_id.items():
        processed_item = processed.get(meeting_id)
        status = 'processed' if processed_item else item.get('status', 'unprocessed')
        title = item.get('title') or item.get('name') or (processed_item or {}).get('title') or meeting_id
        normalized.append({
            'id': meeting_id,
            'title': title,
            'recorded_at': item.get('recorded_at') or item.get('start_at') or item.get('created_at') or '',
            'created_at': item.get('created_at', ''),
            'duration': item.get('duration', 0),
            'status': status,
            'summary': (processed_item or {}).get('summary', item.get('summary', '')),
            'task_count': item.get('task_count', 0),
            'last_action_at': item.get('last_action_at', ''),
            'transcript_path': item.get('transcript_path') or (processed_item or {}).get('transcript_path', ''),
            'transcript_chars': item.get('transcript_chars') or (processed_item or {}).get('transcript_chars', 0),
            'transcript_saved_at': item.get('transcript_saved_at') or (processed_item or {}).get('transcript_saved_at', ''),
            'transcript_preview': item.get('transcript_preview') or (processed_item or {}).get('transcript_preview', ''),
        })

    normalized.sort(key=lambda m: m.get('recorded_at') or m.get('created_at') or '', reverse=True)
    unprocessed_count = sum(1 for m in normalized if m.get('status') != 'processed')
    return {
        'updated_at': cache.get('updated_at') or '',
        'sync_status': cache.get('sync_status', ''),
        'sync_requested_at': cache.get('sync_requested_at', ''),
        'meetings': normalized,
        'kpis': {
            'total': len(normalized),
            'processed': len(normalized) - unprocessed_count,
            'unprocessed': unprocessed_count,
        },
    }


def update_meeting_cache(meeting_id, fields):
    MEETINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache = read_json_file(MEETINGS_FILE, {'meetings': []})
    meetings = cache.get('meetings') if isinstance(cache.get('meetings'), list) else []
    found = False
    for item in meetings:
        if item.get('id') == meeting_id:
            item.update(fields)
            found = True
            break
    if not found:
        meetings.append({'id': meeting_id, **fields})
    cache['meetings'] = meetings
    cache['updated_at'] = now_iso()
    write_json_file(MEETINGS_FILE, cache)
    return cache


def send_mcp_message(proc, payload):
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
    proc.stdin.flush()


def wait_mcp_response(message_queue, request_id, timeout=30):
    deadline = time.time() + timeout
    stderr_lines = []
    while time.time() < deadline:
        try:
            source, line = message_queue.get(timeout=min(0.25, max(deadline - time.time(), 0.01)))
        except queue.Empty:
            continue

        if source == 'stderr':
            if line:
                stderr_lines.append(line)
            continue

        try:
            message = json.loads(line)
        except Exception:
            continue
        if message.get('id') == request_id:
            return message

    detail = stderr_lines[-1] if stderr_lines else '응답 시간 초과'
    raise RuntimeError(f'Plaud MCP 응답을 받지 못했습니다: {detail}')


def call_plaud_mcp_tool(tool_name, arguments=None, timeout=60):
    message_queue = queue.Queue()
    proc = subprocess.Popen(
        [resolve_npx_command(), '-y', '@plaud-ai/mcp@latest'],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=1,
    )

    def read_stream(stream, source):
        for line in stream:
            message_queue.put((source, line.rstrip('\n')))

    threading.Thread(target=read_stream, args=(proc.stdout, 'stdout'), daemon=True).start()
    threading.Thread(target=read_stream, args=(proc.stderr, 'stderr'), daemon=True).start()

    try:
        send_mcp_message(proc, {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2025-06-18',
                'capabilities': {},
                'clientInfo': {'name': 'crata-dashboard', 'version': '1.0'},
            },
        })
        init_response = wait_mcp_response(message_queue, 1, timeout=20)
        if init_response.get('error'):
            raise RuntimeError(init_response['error'].get('message') or 'Plaud MCP 초기화 실패')

        send_mcp_message(proc, {'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}})
        send_mcp_message(proc, {
            'jsonrpc': '2.0',
            'id': 2,
            'method': 'tools/call',
            'params': {'name': tool_name, 'arguments': arguments or {}},
        })
        response = wait_mcp_response(message_queue, 2, timeout=timeout)
        if response.get('error'):
            raise RuntimeError(response['error'].get('message') or f'Plaud MCP {tool_name} 호출 실패')
        return response.get('result') or {}
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def plaud_tool_text_payload(result):
    if result.get('isError'):
        text = ' / '.join(
            (item.get('text') or '').strip()
            for item in result.get('content') or []
            if item.get('type') == 'text' and item.get('text')
        )
        raise RuntimeError(f'Plaud MCP 오류 응답: {text[:500] or "내용 없음"}')

    text_values = []
    for item in result.get('content') or []:
        if item.get('type') == 'text' and item.get('text'):
            text = item['text'].strip()
            if not text:
                continue
            text_values.append(text)
            candidates = [text]
            fenced = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL | re.IGNORECASE)
            if fenced:
                candidates.append(fenced.group(1).strip())
            for opener, closer in (('{', '}'), ('[', ']')):
                start = text.find(opener)
                end = text.rfind(closer)
                if start >= 0 and end > start:
                    candidates.append(text[start:end + 1])
            for candidate in candidates:
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue

    sample = ' / '.join(text_values)[:500]
    raise RuntimeError(f'Plaud MCP 응답 JSON 파싱 실패: {sample or "텍스트 payload 없음"}')


def format_transcript_time(value):
    try:
        milliseconds = int(float(value))
    except Exception:
        return ''
    total_seconds = max(milliseconds // 1000, 0)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def collect_transcript_segments(value):
    segments = []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            return collect_transcript_segments(json.loads(text))
        except Exception:
            return [{'content': text}]

    if isinstance(value, dict):
        if isinstance(value.get('data_content'), str):
            segments.extend(collect_transcript_segments(value.get('data_content')))
        for key in ('source_list', 'transcript', 'transcripts', 'segments', 'items', 'data'):
            if key in value:
                segments.extend(collect_transcript_segments(value.get(key)))
        if value.get('content') or value.get('text'):
            segments.append(value)
        return segments

    if isinstance(value, list):
        for item in value:
            segments.extend(collect_transcript_segments(item))
    return segments


def transcript_segments_to_text(segments):
    lines = []
    for segment in segments:
        content = str(segment.get('content') or segment.get('text') or '').strip()
        if not content:
            continue
        time_label = format_transcript_time(segment.get('start_time') or segment.get('start') or segment.get('start_ms') or 0)
        speaker = str(segment.get('speaker') or segment.get('original_speaker') or '').strip()
        prefix_parts = []
        if time_label:
            prefix_parts.append(time_label)
        if speaker:
            prefix_parts.append(speaker)
        prefix = f"[{' | '.join(prefix_parts)}] " if prefix_parts else ''
        lines.append(f'{prefix}{content}')
    return '\n\n'.join(lines).strip()


def fetch_and_store_meeting_transcript(meeting_id):
    state = read_meetings_state()
    meeting = next((m for m in state.get('meetings', []) if m.get('id') == meeting_id), {})
    result = call_plaud_mcp_tool('get_transcript', {'file_id': meeting_id}, timeout=120)
    payload = plaud_tool_text_payload(result)
    segments = collect_transcript_segments(payload)
    text = transcript_segments_to_text(segments)
    if not text:
        raise RuntimeError('Plaud 전사록 응답에 표시할 본문이 없습니다.')

    transcript_path = transcript_file_for(meeting_id)
    if not transcript_path:
        raise RuntimeError('전사록 저장 경로를 만들 수 없습니다.')
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(text, encoding='utf-8')

    display = display_path(transcript_path)
    saved_at = now_iso()
    update_meeting_cache(meeting_id, {
        'title': meeting.get('title') or meeting_id,
        'recorded_at': meeting.get('recorded_at', ''),
        'created_at': meeting.get('created_at', ''),
        'duration': meeting.get('duration', 0),
        'transcript_path': display,
        'transcript_chars': len(text),
        'transcript_saved_at': saved_at,
        'transcript_preview': text[:700],
    })

    return {
        'ok': True,
        'id': meeting_id,
        'transcript': text,
        'chars': len(text),
        'path': display,
        'saved_at': saved_at,
    }


def fetch_plaud_recordings_from_mcp():
    all_items = []
    seen = set()
    page_size = 100
    for page in range(1, 6):
        result = call_plaud_mcp_tool('list_files', {'page': page, 'page_size': page_size}, timeout=75)
        payload = plaud_tool_text_payload(result)
        items = payload.get('data') if isinstance(payload, dict) else []
        if not isinstance(items, list):
            raise RuntimeError('Plaud MCP list_files 응답 형식이 올바르지 않습니다.')
        for item in items:
            meeting_id = item.get('id')
            if meeting_id and meeting_id not in seen:
                seen.add(meeting_id)
                all_items.append(item)
        if len(items) < page_size:
            break
    return all_items


def sync_plaud_meetings_direct():
    processed = read_processed_recordings()
    cache = read_json_file(MEETINGS_FILE, {'meetings': []})
    existing_by_id = {
        item.get('id'): item
        for item in cache.get('meetings', [])
        if isinstance(item, dict) and item.get('id')
    }
    preserved_keys = (
        'summary',
        'task_count',
        'last_action_at',
        'transcript_path',
        'transcript_chars',
        'transcript_saved_at',
        'transcript_preview',
    )
    meetings = []
    for item in fetch_plaud_recordings_from_mcp():
        meeting_id = item.get('id')
        if not meeting_id:
            continue
        existing = existing_by_id.get(meeting_id, {})
        meeting = {
            'id': meeting_id,
            'title': item.get('name') or item.get('title') or existing.get('title') or meeting_id,
            'recorded_at': item.get('start_at') or item.get('recorded_at') or existing.get('recorded_at') or '',
            'created_at': item.get('created_at') or existing.get('created_at') or '',
            'duration': item.get('duration') or existing.get('duration') or 0,
            'status': 'processed' if meeting_id in processed else existing.get('status', 'unprocessed'),
        }
        for key in preserved_keys:
            if existing.get(key):
                meeting[key] = existing[key]
        meetings.append(meeting)

    write_json_file(MEETINGS_FILE, {
        'updated_at': now_iso(),
        'sync_status': 'done',
        'sync_requested_at': cache.get('sync_requested_at', ''),
        'meetings': meetings,
    })
    return read_meetings_state()


def mark_meeting_sync_error(error):
    cache = read_json_file(MEETINGS_FILE, {'meetings': []})
    cache['updated_at'] = now_iso()
    cache['sync_status'] = 'error'
    cache['sync_error'] = str(error)
    cache.setdefault('meetings', [])
    write_json_file(MEETINGS_FILE, cache)
    return read_meetings_state()


def run_meeting_sync_task(fpath):
    if task_is_cancelled(fpath):
        return

    update_task_file(fpath, lambda t: (
        t.update({
            'status': 'active',
            'statusLbl': '진행 중',
            'progress': 20,
            'started_at': now_iso(),
            'runner': {
                'active': 'server',
                'primary': 'server',
                'fallback': None,
                'command': resolve_npx_command(),
                'mode': 'direct Plaud MCP list_files',
            },
            'error': '',
            'result': '서버가 Plaud MCP list_files로 회의록 목록을 갱신하고 있습니다.',
        }),
        append_task_log(t, '시스템', '서버 직접 Plaud MCP 동기화를 시작했습니다.')
    ))

    try:
        sync_plaud_meetings_direct()
        update_task_file(fpath, lambda t: (
            t.update({
                'status': 'done',
                'statusLbl': '완료됨',
                'progress': 100,
                'completed_at': now_iso(),
            }),
            enrich_task_result(t, '서버가 Plaud MCP list_files를 직접 호출해 목록 캐시를 갱신했습니다.'),
            append_task_log(t, '시스템', 'Plaud 회의록 목록 동기화가 완료되었습니다.')
        ))
    except Exception as exc:
        mark_meeting_sync_error(exc)
        update_task_file(fpath, lambda t: (
            t.update({
                'status': 'error',
                'statusLbl': '실패',
                'progress': 100,
                'error': f'PLAUD 목록 동기화 실패: {exc}',
                'completed_at': now_iso(),
            }),
            enrich_task_result(t, f'서버 직접 Plaud MCP 동기화 실패: {exc}'),
            append_task_log(t, '시스템', 'Plaud 회의록 목록 동기화에 실패했습니다.')
        ))


def start_meeting_sync_worker(fpath):
    thread = threading.Thread(target=run_meeting_sync_task, args=(pathlib.Path(fpath),), daemon=True)
    thread.start()
    return thread


def transcript_file_for(meeting_id):
    safe_id = re.sub(r'[^0-9A-Za-z_.-]+', '_', str(meeting_id or '').strip())
    if not safe_id:
        return None
    return TRANSCRIPTS_DIR / f'{safe_id}.txt'


def display_path(fpath):
    try:
        return str(pathlib.Path(fpath).relative_to(ROOT)).replace('\\', '/')
    except Exception:
        return str(fpath).replace('\\', '/')


def read_meeting_transcript(meeting_id):
    if not meeting_id:
        return {'ok': False, 'error': 'meeting id required', 'transcript': ''}

    state = read_meetings_state()
    normalized_meeting = next((m for m in state.get('meetings', []) if m.get('id') == meeting_id), {})
    cache = read_json_file(MEETINGS_FILE, {'meetings': []})
    raw_meeting = next((m for m in cache.get('meetings', []) if m.get('id') == meeting_id), {})
    processed_item = read_processed_recordings().get(meeting_id, {})
    meeting = {**processed_item, **raw_meeting, **normalized_meeting}
    candidates = []
    for key in ('transcript_path', 'transcriptFile', 'transcript_file'):
        raw = meeting.get(key)
        if raw:
            fpath = pathlib.Path(raw)
            candidates.append(fpath if fpath.is_absolute() else ROOT / fpath)

    default_path = transcript_file_for(meeting_id)
    if default_path:
        candidates.append(default_path)

    seen = set()
    for fpath in candidates:
        try:
            resolved = pathlib.Path(fpath)
            marker = str(resolved)
            if marker in seen:
                continue
            seen.add(marker)
            if resolved.is_file():
                text = resolved.read_text(encoding='utf-8')
                return {
                    'ok': True,
                    'id': meeting_id,
                    'transcript': text,
                    'chars': len(text),
                    'path': display_path(resolved),
                    'saved_at': datetime.fromtimestamp(resolved.stat().st_mtime).isoformat(),
                }
        except Exception as exc:
            return {'ok': False, 'id': meeting_id, 'error': str(exc), 'transcript': ''}

    for key in ('transcript', 'transcript_text', 'transcriptText', 'full_transcript', 'transcript_preview'):
        text = meeting.get(key)
        if text:
            return {
                'ok': True,
                'id': meeting_id,
                'transcript': text,
                'chars': len(text),
                'path': meeting.get('transcript_path', ''),
                'saved_at': meeting.get('transcript_saved_at') or meeting.get('last_action_at', ''),
            }

    try:
        return fetch_and_store_meeting_transcript(meeting_id)
    except Exception as exc:
        return {'ok': False, 'id': meeting_id, 'error': str(exc), 'transcript': ''}


def meeting_snapshot():
    state = read_meetings_state()
    meetings = state.get('meetings', [])
    latest = meetings[0] if meetings else {}
    return {
        'updated_at': state.get('updated_at', ''),
        'sync_status': state.get('sync_status', ''),
        'total': state.get('kpis', {}).get('total', len(meetings)),
        'processed': state.get('kpis', {}).get('processed', 0),
        'unprocessed': state.get('kpis', {}).get('unprocessed', 0),
        'latest_title': latest.get('title', ''),
        'latest_created_at': latest.get('created_at', ''),
        'latest_recorded_at': latest.get('recorded_at', ''),
        'ids': [m.get('id') for m in meetings if m.get('id')],
        'titles': {m.get('id'): m.get('title', '') for m in meetings if m.get('id')},
    }


def prepend_summary(summary, cli_output):
    cli_output = (cli_output or '').strip()
    if not cli_output:
        return summary
    return f'{summary}\n\n--- CLI 원문 ---\n{cli_output}'


def enrich_task_result(task, cli_output):
    task_type = task.get('type')

    if task_type == 'plaud_sync':
        before = task.get('meetingSyncBefore') or {}
        after = meeting_snapshot()
        can_compare = isinstance(before.get('ids'), list)
        before_ids = set(before.get('ids') or [])
        after_ids = set(after.get('ids') or [])
        new_ids = sorted(after_ids - before_ids)
        removed_ids = sorted(before_ids - after_ids)
        title_map = after.get('titles') or {}
        new_titles = [title_map.get(mid, mid) for mid in new_ids[:8]]
        before_total = before.get('total') if can_compare else '이전값 없음'
        before_processed = before.get('processed') if can_compare else '이전값 없음'
        before_unprocessed = before.get('unprocessed') if can_compare else '이전값 없음'

        lines = [
            'PLAUD 회의록 목록 업데이트 결과',
            f"- 전체 회의록: {before_total} → {after.get('total', 0)}건",
            f"- 처리 완료: {before_processed} → {after.get('processed', 0)}건",
            f"- 미처리: {before_unprocessed} → {after.get('unprocessed', 0)}건",
        ]
        if can_compare:
            lines.append(f"- 새로 불러온 회의록: {len(new_ids)}건")
            lines.append(f"- 사라진 회의록: {len(removed_ids)}건")
            if new_titles:
                lines.append('- 신규 항목: ' + ' / '.join(new_titles))
            elif not new_ids:
                lines.append('- 신규 항목: 없음')
        else:
            lines.append('- 새로 불러온 회의록: 이전 스냅샷이 없어 계산하지 못했습니다.')
        if after.get('latest_title'):
            lines.append(f"- 최신 회의록: {after.get('latest_title')}")
            lines.append(f"- 최신 생성 시각: {after.get('latest_created_at') or after.get('latest_recorded_at') or '알 수 없음'}")
        stale_sync = after.get('sync_status') == 'requested'
        lines.append(f"- 목록 상태: {after.get('sync_status') or 'unknown'}")
        if stale_sync:
            task['status'] = 'error'
            task['statusLbl'] = '실패'
            task['error'] = 'PLAUD 목록 동기화가 완료되지 않았습니다. plaud_meetings.json의 sync_status가 requested 상태로 남았습니다.'
            lines.append('- 동기화 오류: 작업은 종료됐지만 목록 캐시가 requested 상태로 남았습니다.')

        task['syncResult'] = {
            'before': before,
            'after': after,
            'newCount': len(new_ids),
            'removedCount': len(removed_ids),
            'newTitles': new_titles,
            'syncIncomplete': stale_sync,
        }
        task['result'] = prepend_summary('\n'.join(lines), cli_output)
        return

    if task_type == 'plaud_process':
        meeting_id = task.get('meetingId')
        state = read_meetings_state()
        meeting = next((m for m in state.get('meetings', []) if m.get('id') == meeting_id), {})
        source_tasks = [
            t for t in read_inbox_tasks()
            if t.get('sourceMeetingId') == meeting_id and t.get('id') != task.get('id')
        ]
        task_count = meeting.get('task_count') or len(source_tasks)
        category_missing = [
            t.get('title', t.get('id', ''))
            for t in source_tasks
            if not t.get('categoryId') or not t.get('categoryName')
        ]
        transcript_chars = meeting.get('transcript_chars') or 0
        transcript_path = meeting.get('transcript_path') or ''
        transcript_saved_label = f'{transcript_path or "없음"} ({transcript_chars}자)' if transcript_chars else (transcript_path or '없음')
        lines = [
            'PLAUD 회의록 업무 분해 결과',
            f"- 대상 회의록: {task.get('meetingTitle') or task.get('title')}",
            '- 전사록 처리 방식: Plaud get_transcript의 전체 전사록을 CLI 분석 입력으로 사용',
            f"- 전사록 저장: {transcript_saved_label}",
            f"- 생성 업무: {task_count}건",
            f"- 회의록 상태: {meeting.get('status') or 'unknown'}",
            f"- 카테고리 누락 업무: {len(category_missing)}건",
        ]
        if source_tasks:
            lines.append('- 생성된 업무: ' + ' / '.join(t.get('title', t.get('id', '')) for t in source_tasks[:8]))
        if category_missing:
            lines.append('- 카테고리 누락 항목: ' + ' / '.join(category_missing[:8]))

        task['processResult'] = {
            'meetingId': meeting_id,
            'meetingStatus': meeting.get('status', ''),
            'taskCount': task_count,
            'categoryMissingCount': len(category_missing),
        }
        task['result'] = prepend_summary('\n'.join(lines), cli_output)


def create_meeting_sync_task(data):
    before_snapshot = meeting_snapshot()
    cache = read_json_file(MEETINGS_FILE, {'meetings': []})
    cache['sync_status'] = 'requested'
    cache['sync_requested_at'] = now_iso()
    cache.setdefault('meetings', [])
    MEETINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(MEETINGS_FILE, cache)

    try:
        state = sync_plaud_meetings_direct()
    except Exception as exc:
        state = mark_meeting_sync_error(exc)
        return {
            'ok': False,
            'error': f'PLAUD 목록 동기화 실패: {exc}',
            'meetings': state,
        }

    after_snapshot = meeting_snapshot()
    before_ids = set(before_snapshot.get('ids') or [])
    after_ids = set(after_snapshot.get('ids') or [])
    new_ids = sorted(after_ids - before_ids)
    removed_ids = sorted(before_ids - after_ids)
    return {
        'ok': True,
        'meetings': state,
        'syncResult': {
            'before': before_snapshot,
            'after': after_snapshot,
            'newCount': len(new_ids),
            'removedCount': len(removed_ids),
        },
        'message': f"PLAUD 회의록 {after_snapshot.get('total', 0)}건으로 갱신했습니다.",
    }


def create_meeting_process_task(data):
    meeting_id = data.get('id') or data.get('meetingId')
    if not meeting_id:
        return {'ok': False, 'error': 'meeting id required'}

    meetings = read_meetings_state().get('meetings', [])
    meeting = next((m for m in meetings if m.get('id') == meeting_id), {})
    title = data.get('title') or meeting.get('title') or meeting_id
    recorded_at = data.get('recorded_at') or meeting.get('recorded_at') or ''
    runner_preference = data.get('runnerPreference') if data.get('runnerPreference') in ('claude', 'codex') else 'claude'
    transcript_path = transcript_file_for(meeting_id)
    transcript_path_display = display_path(transcript_path) if transcript_path else ''

    if meeting.get('status') == 'processed':
        return {'ok': False, 'error': '이미 처리 완료된 회의록입니다.'}

    update_meeting_cache(meeting_id, {
        'title': title,
        'recorded_at': recorded_at,
        'status': 'processing',
        'last_action_at': now_iso(),
    })

    desc = f"""PLAUD 회의록 처리 작업입니다.

파일 ID: {meeting_id}
제목: {title}
녹음 시각: {recorded_at}

처리 방식:
1. Plaud MCP의 get_transcript로 이 파일의 전사록 전체를 읽으세요. 일부 요약본이나 앞부분만 보고 판단하지 마세요.
2. 읽은 전사록 전체를 {transcript_path_display}에 UTF-8 텍스트로 저장하세요.
3. 처리관리/녹음/plaud_meetings.json의 해당 회의록에 transcript_path="{transcript_path_display}", transcript_chars, transcript_saved_at, transcript_preview(앞 700자)를 기록하세요.
4. 받은 전사록 전체를 CLI 분석 입력으로 삼아 CRATA 관련 작업 지시, 결정 후보, 참고 내용을 빠짐없이 추출하세요.
5. 실행 가능한 내용은 세분화된 업무 카드로 나누세요. 서로 다른 산출물/파일/판단 단위는 별도 업무로 분리하세요.
6. 모든 세분화 업무는 반드시 처리관리/categories.json의 카테고리에 넣으세요. 맞는 카테고리가 없으면 기존 카테고리 목록을 보고 새 카테고리를 추가한 뒤 그 카테고리를 지정하세요.
7. 각 세분화 업무는 처리관리/inbox/task_YYYYMMDD_HHMMSS_번호.json 형태의 대기 작업으로 생성하세요. 각 작업에는 id, title, desc, status=pending, statusLbl=대기 중, priority, priorityLbl, agent, agentName, date, progress, categoryId, categoryName, sourceMeetingId="{meeting_id}", sourceMeetingTitle, collabLogs를 포함하세요.
8. 전사록 안에 날짜·시간·마감·회의 일정 단서가 있으면 해당 업무 JSON에 scheduleText 또는 scheduleHint를 추가하세요. 캘린더에 바로 저장하지 말고, 사용자가 대시보드에서 확인 후 일정으로 잡을 수 있도록 단서만 남기세요.
9. 문구·지식 원본 파일은 바로 수정하지 마세요. 필요한 수정은 작업 카드 안에 제안/확인 필요로 남기세요.
10. 처리관리/녹음/plaud_meetings.json에서 이 회의록의 status를 "extracted"로 바꾸고 task_count를 기록하세요. 사용자 확인 전에는 plaud_processed.json을 완료 처리로 업데이트하지 마세요.
11. 대시보드 결과에는 읽은 전사록의 대략적 글자 수, 생성한 업무 수, 업무 제목·카테고리, 일정 단서가 있는 업무, 추가 확인이 필요한 결정을 간단히 남기세요.
"""
    task, _ = save_task_request({
        'title': f'회의록 업무 분해: {title[:32]}',
        'desc': desc,
        'type': 'plaud_process',
        'priority': 'high',
        'priorityLbl': '긴급',
        'agent': 'plan',
        'agentName': '기획담당',
        'runnerPreference': runner_preference,
        'categoryId': 'meeting-minutes',
        'categoryName': '회의록 처리',
        'meetingId': meeting_id,
        'meetingTitle': title,
        'transcriptMode': 'full_transcript_to_cli',
        'assignments': [
            {'name': '기획담당', 'role': '전사록 읽기 및 업무 분해', 'progress': 0, 'status': 'pending'},
        ],
    }, start=True)
    return {'ok': True, 'task': task, 'meetings': read_meetings_state()}


def create_video_encoding_task(data):
    source_path = str(data.get('sourcePath') or data.get('path') or '').strip()
    files, error = collect_video_source_files(source_path)
    if error:
        return {'ok': False, 'error': error}

    steps = data.get('steps') if isinstance(data.get('steps'), list) else []
    step_ids = []
    step_labels = []
    for step in steps:
        if isinstance(step, dict):
            step_id = str(step.get('id') or '').strip()
            label = str(step.get('label') or step.get('id') or '').strip()
        else:
            step_id = ''
            label = str(step or '').strip()
        if step_id:
            step_ids.append(step_id)
        if label:
            step_labels.append(label)
    if not step_labels:
        return {'ok': False, 'error': '실행할 인코딩 단계를 하나 이상 선택하세요.'}
    needs_preview = any(step in step_ids for step in ('burnin', 'encode')) or any(label in step_labels for label in ('자막 하드코딩', '최종 인코딩'))
    if needs_preview and 'preview' not in step_ids and '미리보기 검수' not in step_labels:
        id_insert_at = next((idx for idx, step in enumerate(step_ids) if step in ('burnin', 'encode')), len(step_labels))
        label_insert_at = next((idx for idx, label in enumerate(step_labels) if label in ('자막 하드코딩', '최종 인코딩')), len(step_labels))
        insert_at = min(id_insert_at, label_insert_at)
        insert_at = min(insert_at, len(step_labels))
        step_ids.insert(insert_at, 'preview')
        step_labels.insert(insert_at, '미리보기 검수')

    preset = str(data.get('preset') or 'fast').strip()
    preset_label = str(data.get('presetLabel') or preset).strip()
    subtitle_style = normalize_video_subtitle_style(data.get('subtitleStyle') if isinstance(data.get('subtitleStyle'), dict) else read_video_subtitle_style())
    runner_preference = data.get('runnerPreference') if data.get('runnerPreference') in ('claude', 'codex') else 'codex'
    speaker_count = None
    speaker_count_raw = data.get('speakerCount') if data.get('speakerCount') is not None else data.get('speaker_count')
    try:
        speaker_count = int(speaker_count_raw)
    except (TypeError, ValueError):
        speaker_count = None
    if speaker_count is not None and not 1 <= speaker_count <= 12:
        return {'ok': False, 'error': '화자 수는 1명 이상 12명 이하로 입력하세요.'}
    if ('diarize' in step_ids or '화자분리' in step_labels) and speaker_count is None:
        return {'ok': False, 'error': '화자분리 단계 실행 전 화자 수를 선택하세요.'}
    speaker_count_label = f'{speaker_count}명' if speaker_count is not None else '미지정'
    file_preview = [str(path) for path in files[:20]]
    omitted_count = max(len(files) - len(file_preview), 0)
    workspace_root = video_workspace_dir()

    status = {
        'status': 'requested',
        'progress': 0,
        'current_file': str(files[0]) if len(files) == 1 else source_path,
        'batch_progress': 0,
        'total_files': len(files),
        'completed_files': [],
        'current_step': 0,
        'requested_at': now_iso(),
        'source_path': source_path,
        'original_source_path': source_path,
        'workspace_root': str(workspace_root),
        'preset': preset,
        'preset_label': preset_label,
        'subtitle_style': subtitle_style,
        'steps': step_labels,
        'speaker_count': speaker_count,
        'review_required': True,
        'preview_required': needs_preview,
        'preview_confirmation_required': needs_preview,
        'current_process': 'raw_transcribe',
        'current_process_label': '원본 전사 대기',
        'process_steps': VIDEO_PROCESS_STEPS,
        'process_status': {},
        'process_results': {},
        'artifacts': [],
        'system_metrics': collect_system_metrics(),
        'model_status': {
            'active_model': 'large-v3',
            'device': 'cuda',
            'compute': 'float16',
            'task': '원본 전사 대기',
            'download_status': '확인 대기',
            'download_progress': 0,
        },
        'model_download': {
            'name': 'large-v3',
            'status': '확인 대기',
            'progress': 0,
            'message': '첫 실행이면 faster-whisper 모델 다운로드가 진행될 수 있습니다.',
        },
        'review_order': ['raw_transcribe', 'diarize', 'transcript_quality_review', 'crata_term_correction', 'speaker_review', 'subtitle_preview_review', 'final_encode'],
    }
    write_json_file(VIDEO_STATUS_FILE, status)

    desc = f"""데스크탑 영상 인코딩 작업입니다.

중요:
- 이 작업은 시뮬레이션이 아닙니다. 실제 처리 가능한 로컬 파이프라인을 찾아 실행하세요.
- 우선 repo 안의 .agents/skills/video-encoding/SKILL.md를 읽고 그 절차를 따르세요.
- 실행 스크립트는 tools/video_agent/subtitle_agent.py를 우선 사용하세요. 아직 이관 전이면 C:\\Users\\wnsdu\\Desktop\\프로젝트\\영상편집에이전트\\subtitle_agent.py를 확인하세요.
- tools/video_agent/subtitle_agent.py는 전사 세그먼트가 생성될 때마다 raw_transcribe 진행률을 갱신하도록 되어 있으니, 진행률 표시가 필요한 작업에서는 이 내부 스크립트를 우선하세요.
- 작업 시작 전 `python tools/video_agent/subtitle_agent.py prepare --source "{source_path}" --workspace-dir "{workspace_root}"`로 새 작업 폴더를 만들고 요청 영상 파일 원본을 그 폴더 안으로 이동하세요. 대용량 원본 영상이 원래 위치와 작업 폴더에 중복으로 남으면 안 됩니다.
- prepare가 출력한 "준비된 작업 경로"를 이후 모든 subtitle_agent.py 명령의 `--base-dir` 값으로 사용하세요.
- 전사 완료 후 내부 스크립트가 전사록 전체의 반복 핵심 용어, 검사명, 역량명을 분석해 짧은 주제형 제목을 정하고 작업 폴더명, 영상 파일명, 원본 전사 파일명을 공통 stem으로 확정합니다. 자막 한 줄이나 문장형 발화를 그대로 파일명으로 쓰지 마세요. 폴더명이나 파일명이 바뀌면 `video_status.json`의 `workspace_path` / `source_path` / `current_file`에 기록된 새 경로로 이후 단계(`review`, `diarize`, `preview`, `hardcode`, `final`)를 계속하세요.
- 전사록, 품질검토 리포트, ASS/SRT, 미리보기, 하드코딩 결과, 최종 인코딩 파일은 모두 해당 작업 폴더 안에 저장하고, 확정된 공통 stem에 `_review`, `_colored`, `_preview`, `_sub`, `_final` 같은 단계 suffix를 붙이세요. 원본 소스 폴더에는 영상 사본이나 산출물을 남기지 마세요.
- ffmpeg, faster-whisper large-v3, pyannote diarization, subtitle burn-in, final encode 관련 실행 가능성을 확인하세요.
- 실행 가능한 파이프라인이 없으면 임의 완료 처리하지 말고 필요한 스크립트/의존성/명령을 결과에 명확히 보고하세요.
- 진행 중에는 video_status.json을 갱신하세요. 형식은 status(active|requested|waiting_preview_review|done|error), progress, current_file, batch_progress, total_files, completed_files, current_process, current_process_label, process_status, message, speaker_count 입니다.
- 기존 영상편집에이전트가 C:\\Users\\wnsdu\\OneDrive\\대시보드\\video_status.json에 진행률을 쓰는 경우도 대시보드가 읽습니다. 가능하면 프로젝트 루트의 video_status.json을 직접 갱신하고, 기존 스크립트를 쓰면 해당 legacy 상태 파일도 계속 갱신되게 두세요.
- 모델 다운로드 중에는 model_download.name, model_download.status, model_download.progress를 갱신하세요.
- 현재 사용 중인 모델은 model_status.active_model, model_status.device, model_status.compute, model_status.task로 남기세요.
- 각 프로세스 산출물은 process_results.<process_id> 또는 artifacts에 파일 경로와 설명을 남기세요. 대시보드에서 프로세스를 클릭하면 이 값들이 표시됩니다.
- process_results에는 가능한 한 아래 형태를 사용하세요: {{"title":"원본 전사록","path":"...srt","kind":"전사록","viewer":"transcript"}}. 전사 품질검토, CRATA 용어 교정, 화자분리 검토처럼 수정/확인 내역이 있는 단계는 changes 배열에 {{"before":"기존 문장 또는 기존 화자","after":"변경 문장 또는 변경 화자","reason":"근거","segment":"00:01:23"}} 형태로 남기세요. 대시보드는 이를 "기존 → 변경"으로 표시합니다.
- 화자분리 검토까지 끝난 최종 검토 전사록은 _final_reviewed.srt 또는 _speaker_reviewed.srt로 저장하고 process_results.speaker_review에 viewer="transcript"로 남기세요. 이 파일이 대시보드의 최종 전사록 검토 패널에 표시됩니다.
- 최종 전사록 검토 화면에는 화자 구분이 보여야 합니다. 검토용 SRT에는 [화자1]/[강사]/[질문자] 같은 화자 표기를 보존하거나, ASS의 Name 필드에 화자명을 남기세요. 단, 실제 최종 자막 하드코딩 전에는 표시 텍스트에서 이 접두어를 제거하세요.
- MXF는 웹 미리보기가 안 될 수 있지만 ffmpeg 입력으로는 처리 가능할 수 있습니다.

소스 경로:
{source_path}

작업 폴더 루트:
{workspace_root}

대상 파일 수: {len(files)}
대상 파일 미리보기:
{chr(10).join('- ' + path for path in file_preview)}
{f'- ... 외 {omitted_count}개' if omitted_count else ''}

선택 단계:
{', '.join(step_labels)}

화자 수:
{speaker_count_label}

출력 품질/용도:
{preset_label} ({preset})

자막 스타일:
- 화자 접두어 표시: 사용하지 않음. [강사], [질문자] 같은 표시는 최종 자막에 넣지 마세요.
- 화자별 색상: 사용하지 않음. 모든 자막은 흰색으로 통일하세요.
- 글꼴: {subtitle_style.get('font_name')}
- 크기: {subtitle_style.get('font_size')}
- 굵게: {'사용' if subtitle_style.get('bold') else '사용 안 함'}
- 외곽선: {subtitle_style.get('outline')}
- 그림자: {subtitle_style.get('shadow')}
- 하단 여백: {subtitle_style.get('margin_v')}

필수 품질검토 순서:
1. 원본 전사: faster-whisper large-v3로 raw SRT/전사본을 생성하세요. 이 원본은 보존합니다.
2. 화자분리: 입력된 화자 수({speaker_count_label})를 기준으로 raw 전사 세그먼트에 화자 라벨을 붙이세요.
3. 전사 품질검토: 화자 라벨이 붙은 전사록 전체를 문맥 기준으로 읽고 오인식, 끊김, 반복, 어색한 문장을 검토하세요.
4. CRATA 용어 교정: 지식/과 결과지문구/의 공식 용어를 기준으로 CRATA 관련 단어를 교정하세요. 의미가 바뀔 수 있는 부분은 임의 확정하지 말고 확인 필요로 남기세요.
5. 화자분리 검토: 강사/질문자 라벨이 문맥상 뒤바뀐 구간, 짧은 맞장구, 질문 구간을 확인해 수정하거나 확인 필요로 표시하세요.
6. 최종 검토 전사록: 화자분리 검토까지 반영한 전사록을 _final_reviewed.srt 또는 _speaker_reviewed.srt로 저장하세요. 검토용 파일에는 화자 구분이 보이도록 [화자1]/[강사]/[질문자] 같은 접두어 또는 ASS Name 필드를 남기고, 최종 표시 자막에는 이 접두어를 넣지 않습니다.
7. 자막 미리보기 검수: 최종 검토 전사/ASS로 30~60초 미리보기 클립을 먼저 생성하세요. 자막 크기, 위치, 하단 여백, 줄 수, 화자 색상, 얼굴/자료 화면 가림 여부를 확인할 수 있어야 합니다.
8. 미리보기 승인 대기: 미리보기 파일 경로를 남기고 video_status.json에 status를 waiting_preview_review, preview_file, message로 갱신하세요. 사용자가 확인하기 전에는 최종 인코딩을 진행하지 마세요.
9. 사용자가 미리보기 확인 후 승인한 경우에만 검토 완료된 전사/ASS를 기준으로 자막 하드코딩과 최종 인코딩을 진행하세요.

프로세스 상태 표시 규칙:
- 현재 단계가 바뀔 때마다 current_process를 아래 ID 중 하나로 갱신하세요.
- raw_transcribe: 원본 전사
- diarize: 화자분리
- transcript_quality_review: 전사 품질검토
- crata_term_correction: CRATA 용어 교정
- speaker_review: 화자분리 검토
- subtitle_preview_review: 미리보기 검수
- burnin: 자막 하드코딩
- final_encode: 최종 인코딩
- process_status에는 각 단계별 status(pending|active|waiting|done|error)와 progress를 기록하세요.
- 미리보기 확인 대기 상태에서는 status를 waiting_preview_review, current_process를 subtitle_preview_review로 두세요.

처리 지침:
1. 소스 경로가 데스크탑 서버 기준 실제 경로인지 다시 확인하세요.
2. 먼저 prepare 명령으로 작업 폴더를 만들고 요청 영상 파일 원본을 이동하세요. 이후 원본 경로가 아니라 prepare 결과 경로를 기준으로 처리하세요.
3. 전사 단계가 끝나면 전사록 기준으로 확정된 새 폴더/파일 경로를 확인하고, 이후 명령은 그 새 폴더 경로를 `--base-dir`로 넘기세요.
4. 선택된 단계만 수행하되, 전사 또는 화자분리를 수행했다면 품질검토와 CRATA 용어 교정은 건너뛰지 마세요.
5. 자막 하드코딩 또는 최종 인코딩 단계가 선택되어 있다면 미리보기 검수는 필수입니다.
6. 현재 작업 안에서 사용자 승인을 받을 수 없으면 미리보기 생성 후 확인 대기 상태로 멈추고, 최종 인코딩은 다음 승인 작업에서 진행하세요.
7. 결과 파일 경로와 실패한 파일이 있으면 실패 원인을 남기세요.
8. 완료 후 편집이 필요한 결과물은 처리관리/video_edit_queue.json에 pending_edit 항목으로 추가하세요.
"""

    task, _ = save_task_request({
        'title': f'영상 인코딩: {pathlib.Path(source_path).name or source_path}',
        'desc': desc,
        'type': 'video_encoding',
        'priority': 'normal',
        'priorityLbl': '보통',
        'agent': 'media',
        'agentName': '영상담당',
        'runnerPreference': runner_preference,
        'categoryId': 'media-work',
        'categoryName': '영상/미디어',
        'categoryColor': 'red',
        'videoSourcePath': source_path,
        'videoWorkspaceRoot': str(workspace_root),
        'videoFileCount': len(files),
        'videoSteps': step_labels,
        'videoPreset': preset,
        'videoPresetLabel': preset_label,
        'videoSpeakerCount': speaker_count,
        'videoSubtitleStyle': subtitle_style,
        'videoReviewRequired': True,
        'videoPreviewRequired': needs_preview,
        'assignments': [
            {'name': '영상담당', 'role': '영상 인코딩 파이프라인 실행', 'progress': 0, 'status': 'pending'},
        ],
    }, start=True)
    return {'ok': True, 'task': task, 'videoStatus': video_status_payload()}


def create_video_transcript_review_task(data):
    note = str(data.get('note') or data.get('message') or '').strip()
    artifact_path = str(data.get('artifactPath') or data.get('path') or '').strip()
    if not artifact_path:
        return {'ok': False, 'error': '전사록 파일 경로가 없습니다.'}
    if not note:
        return {'ok': False, 'error': '수정 요청 내용을 입력하세요.'}

    artifact_title = str(data.get('artifactTitle') or data.get('title') or '최종 전사록').strip()
    source_path = str(data.get('sourcePath') or '').strip()
    process_id = normalize_video_process_id(data.get('processId') or data.get('process') or '')
    runner_preference = data.get('runnerPreference') if data.get('runnerPreference') in ('claude', 'codex') else 'codex'

    desc = f"""영상 최종 전사록 수정 요청입니다.

대상 전사록:
{artifact_path}

원본 영상:
{source_path or 'video_status.json의 current_file/source_path를 확인'}

현재 산출물:
{artifact_title}

사용자 수정 요청:
{note}

작업 방식:
1. 대상 전사록 파일 전체를 UTF-8로 읽고, 사용자가 남긴 시간대/문구/화자 단서를 찾으세요.
2. 최종 검토 기준은 "화자분리 검토까지 끝난 전사록"입니다. 기존 원본 .srt는 보존하고, 수정본은 _final_reviewed.srt 또는 _speaker_reviewed.srt처럼 검토본임이 드러나는 이름으로 저장하세요.
3. 검토용 전사록에는 화자 구분이 보여야 하므로 [화자1]/[강사]/[질문자] 같은 접두어 또는 ASS Name 필드를 보존하세요. 다만 최종 표시 자막 하드코딩에는 이 접두어를 넣지 마세요.
4. 전사 품질검토, CRATA 용어 교정, 화자분리 검토에서 문장/용어/화자 라벨 수정 또는 확인 필요 항목이 있으면 각 단계의 process_results 항목에 changes 배열로 {{"before":"기존","after":"변경","reason":"근거","segment":"시간대"}} 형태를 남기세요.
5. 수정 결과 파일과 검토 리포트 경로를 video_status.json의 process_results.speaker_review에 추가하세요. 대시보드가 최종 전사록 검토 패널에서 바로 읽을 수 있어야 합니다.
6. 의미가 달라질 수 있거나 사용자의 추가 확인이 필요한 구간은 임의 확정하지 말고 Codex Output에 후보와 질문을 남기세요.
7. 미리보기 생성이나 최종 인코딩은 사용자의 별도 승인 전에는 진행하지 마세요.
"""

    task, _ = save_task_request({
        'title': f'최종 전사록 수정: {pathlib.Path(artifact_path).name or artifact_title}',
        'desc': desc,
        'type': 'video_transcript_review',
        'priority': 'normal',
        'priorityLbl': '보통',
        'agent': 'media',
        'agentName': '영상담당',
        'runnerPreference': runner_preference,
        'categoryId': 'media-work',
        'categoryName': '영상/미디어',
        'categoryColor': 'red',
        'videoArtifactPath': artifact_path,
        'videoArtifactTitle': artifact_title,
        'videoSourcePath': source_path,
        'videoProcessId': process_id,
        'videoReviewNote': note,
        'assignments': [
            {'name': '영상담당', 'role': '최종 전사록 수정 검토', 'progress': 0, 'status': 'pending'},
        ],
    }, start=True)
    return {'ok': True, 'task': task}


# ── 데이터 파싱 헬퍼 ────────────────────────────────────────────────

def parse_frontmatter(content):
    fm = {}
    m = re.match(r'---\s*\n(.*?)\n---', content, re.DOTALL)
    if m:
        for line in m.group(1).split('\n'):
            if ':' in line:
                k, v = line.split(':', 1)
                fm[k.strip()] = v.strip()
    return fm


def read_section(content, header):
    m = re.search(rf'##\s+{re.escape(header)}\s*\n(.*?)(?:\n##\s|\Z)', content, re.DOTALL)
    return m.group(1).strip() if m else ''


# ── decisions/log 파싱 ──────────────────────────────────────────────

def read_decisions_logs():
    logs = []
    log_dir = ROOT / 'decisions' / 'log'
    if not log_dir.exists():
        return logs

    for fpath in sorted(log_dir.glob('*.md'), reverse=True):
        try:
            content = fpath.read_text(encoding='utf-8')
        except Exception:
            continue

        fm = parse_frontmatter(content)
        what = read_section(content, '무엇을')
        why  = read_section(content, '왜')

        name     = fpath.stem  # e.g. "2026-05-22_16행동유형_정의재작성"
        date_str = name[:10] if len(name) >= 10 else fm.get('날짜', '')
        title    = name[11:].replace('_', ' ') if len(name) > 11 else name

        # plaud 녹음 로그는 '시스템'으로 분류
        if '_plaud_' in name:
            agent, agent_name = 'system', '시스템'
        elif '문구' in content and '결과지문구' in content and '지식' not in content[:200]:
            agent, agent_name = 'phrase', '문구담당'
        else:
            agent, agent_name = 'knowledge', '지식담당'

        # 연결 파일 목록
        files = [f'decisions/log/{fpath.name}']
        target = fm.get('대상', '')
        if target and target not in files:
            files.append(target)

        logs.append({
            'agent':     agent,
            'agentName': agent_name,
            'date':      date_str,
            'title':     title,
            'rationale': why[:400]  if why  else '',
            'changes':   what[:400] if what else '',
            'files':     files,
            'status':    fm.get('상태', '확정'),
        })
    return logs


# ── MASTER.md 커버리지 파싱 ─────────────────────────────────────────

def _slot_status(text_content, raw_block):
    """```text 블록 내용으로 완성/초안/미작성 판단."""
    stripped = text_content.strip()
    if not stripped:
        return '미작성'
    # 초안 마커: 블록 바로 앞 줄에 <!-- 초안 --> 등이 있으면
    if '<!-- 초안' in raw_block or '(초안)' in raw_block:
        return '초안'
    return '완성'


def _extract_meta_value(block, label):
    m = re.search(rf'{re.escape(label)}:\s*`?([^`\n]+)`?', block)
    return m.group(1).strip() if m else ''


def _heading_context(pre_content):
    headings = [
        (len(m.group(1)), m.group(2).strip())
        for m in re.finditer(r'^(#{2,6})\s+(.+)$', pre_content, re.MULTILINE)
    ]
    slot_heading = next((text for level, text in reversed(headings) if level >= 4), '')
    section_heading = next((text for level, text in reversed(headings) if level <= 3), '')
    return slot_heading, section_heading


def _slot_display_name(key, slot_name, position):
    slot_label_map = {
        '맞는환경': '맞는 환경',
        '맞지않는환경': '맞지 않는 환경',
    }
    if key.startswith('ORG02_행동방식_'):
        prefix = '행동방식'
    elif key.startswith('ORG02_사고방식_'):
        prefix = '사고방식'
    elif key.startswith('ORG03_'):
        m = re.match(r'^ORG03_([A-Z]+)_([^_]+)$', key)
        if m:
            type_code, slot_part = m.groups()
            slot_part = slot_label_map.get(slot_part, slot_part)
            return f'{type_code} {slot_part}'
        prefix = '성장구조방식'
    elif key.startswith('ORG04_'):
        prefix = '행동방식 축'
    elif key.startswith('ORG06_'):
        m = re.match(r'^ORG06_([A-Z]+)_(.+)$', key)
        if m:
            type_code, slot_part = m.groups()
            return f'{type_code}_{slot_part}'
        prefix = '16행동유형'
    elif key.startswith('ORG07_'):
        m = re.match(r'^ORG07_([A-Z]+)_(.+)$', key)
        if m:
            type_code, slot_part = m.groups()
            return f'{type_code}_{slot_part}'
        prefix = '현재 행동유형'
    elif key.startswith('ORG08_'):
        m = re.match(r'^ORG08_([A-Z]+)_(.+)$', key)
        if m:
            type_code, slot_part = m.groups()
            return f'{type_code}_{slot_part}'
        prefix = '고유-현재 비교'
    elif key.startswith('ORGV1_P05_'):
        prefix = '구버전 과제수행방식'
    elif key.startswith('ORGV1_P06_'):
        prefix = '구버전 자가분석'
    elif key.startswith('ORGV1_'):
        prefix = '구버전 조직수준'
    else:
        prefix = ''

    if prefix and not slot_name.startswith(prefix):
        return f'{prefix} · {slot_name}'
    return slot_name or key


def parse_master_file(fpath):
    """MASTER.md → 슬롯 리스트 반환. 연결키 기준으로 파싱."""
    try:
        content = fpath.read_text(encoding='utf-8')
    except Exception:
        return [], {}

    slots    = []
    contents = {}

    # 패턴: 연결키 → ```text 블록
    pattern = r'연결키:\s*`([^`]+)`(.*?)```text\n(.*?)```'
    for m in re.finditer(pattern, content, re.DOTALL):
        key       = m.group(1)
        mid_block = m.group(2)   # 연결키와 ```text 사이 텍스트
        text      = m.group(3)
        status    = _slot_status(text, mid_block)

        pre = content[:m.start()]
        slot_heading, section_heading = _heading_context(pre)
        slot_name = slot_heading or key
        section = section_heading or '기타'
        position = _extract_meta_value(mid_block, '결과지 위치')
        same_position = _extract_meta_value(mid_block, '동일 위치 설명')
        display_name = _slot_display_name(key, slot_name, position)

        slots.append({
            'key':     key,
            'name':    display_name,
            'rawName': slot_name,
            'section': section,
            'status':  status,
            'position': position,
            'samePosition': same_position,
        })
        contents[key] = text.strip()

    return slots, contents


def read_coverage():
    tracked_files = [
        ROOT / '결과지문구' / '문제해결방식검사 결과지 화면문구_MASTER.md',
        ROOT / '결과지문구' / '행동동기검사 결과지 화면문구_MASTER.md',
    ]
    file_signature = tuple(
        (str(f), f.stat().st_mtime_ns, f.stat().st_size)
        for f in tracked_files
        if f.exists()
    )
    signature = (COVERAGE_PARSER_VERSION,) + file_signature
    if COVERAGE_CACHE['signature'] == signature and COVERAGE_CACHE['data'] is not None:
        return COVERAGE_CACHE['data']

    coverage = {}

    problem_file = ROOT / '결과지문구' / '문제해결방식검사 결과지 화면문구_MASTER.md'
    if problem_file.exists():
        slots, contents = parse_master_file(problem_file)
        done  = sum(1 for s in slots if s['status'] == '완성')
        draft = sum(1 for s in slots if s['status'] == '초안')
        total = len(slots)
        empty = total - done - draft

        # 섹션별로 묶기
        sec_map = {}
        for s in slots:
            sec_map.setdefault(s['section'], []).append(s)

        coverage['problem'] = {
            'total': total, 'done': done, 'draft': draft, 'empty': empty,
            'pct':      f'{round(done / total * 100) if total else 0}%',
            'fraction': f'{done} / {total} 셀',
            'sections': [{'title': t, 'slots': sl} for t, sl in sec_map.items()],
            'slotContents': contents,
        }

    motivation_file = ROOT / '결과지문구' / '행동동기검사 결과지 화면문구_MASTER.md'
    if motivation_file.exists():
        slots, contents = parse_master_file(motivation_file)
        done  = sum(1 for s in slots if s['status'] == '완성')
        draft = sum(1 for s in slots if s['status'] == '초안')
        total = len(slots)
        empty = total - done - draft

        coverage['motivation'] = {
            'total': total, 'done': done, 'draft': draft, 'empty': empty,
            'pct':      f'{round(done / total * 100) if total else 0}%',
            'fraction': f'{done} / {total} 셀',
            'slots':    slots,
            'slotContents': contents,
        }

    COVERAGE_CACHE['signature'] = signature
    COVERAGE_CACHE['data'] = coverage
    return coverage


# ── 최근 활동 ────────────────────────────────────────────────────────

def read_activities():
    activities = []

    # plaud_processed.json
    plaud_file = ROOT / '처리관리' / '녹음' / 'plaud_processed.json'
    if plaud_file.exists():
        try:
            data = json.loads(plaud_file.read_text(encoding='utf-8'))
            for p in reversed(data.get('processed', [])):
                activities.append({
                    'time':          p.get('processed_at', ''),
                    'agent':         '시스템',
                    'agentLabel':    '시스템',
                    'agentBg':       'rgba(96,165,250,0.08)',
                    'agentColor':    'var(--blue)',
                    'text': f"PLAUD: {p.get('summary', p.get('title', ''))}",
                })
        except Exception:
            pass

    # decisions/log 최근 8건
    log_dir = ROOT / 'decisions' / 'log'
    if log_dir.exists():
        for fpath in sorted(log_dir.glob('*.md'), reverse=True)[:8]:
            name  = fpath.stem
            date  = name[:10] if len(name) >= 10 else ''
            title = name[11:].replace('_', ' ') if len(name) > 11 else name
            if '_plaud_' in name:
                continue  # 이미 위에서 처리
            activities.append({
                'time':       date,
                'agent':      'knowledge',
                'agentLabel': '지식담당',
                'agentBg':    'rgba(16,185,129,0.08)',
                'agentColor': 'var(--green)',
                'text':       title,
            })

    # 최신 순으로 정렬, 10개 제한
    activities.sort(key=lambda x: x['time'], reverse=True)
    return activities[:12]


# ── inbox 작업 목록 ──────────────────────────────────────────────────

def read_inbox_tasks():
    tasks = []
    inbox_dir = ROOT / '처리관리' / 'inbox'
    if inbox_dir.exists():
        for fpath in sorted(inbox_dir.glob('task_*.json'), reverse=True):
            try:
                t = json.loads(fpath.read_text(encoding='utf-8'))
                t['_file'] = fpath.name
                missing_category = not t.get('categoryId') or not t.get('categoryName')
                apply_task_category(t)
                if missing_category:
                    persisted = dict(t)
                    persisted.pop('_file', None)
                    write_json_file(fpath, persisted)
                tasks.append(t)
            except Exception:
                pass
    return merge_duplicate_task_list(tasks)


# ── 전체 데이터 ──────────────────────────────────────────────────────

def get_all_data():
    logs       = read_decisions_logs()
    coverage   = read_coverage()
    activities = read_activities()
    tasks      = read_inbox_tasks()
    meetings   = read_meetings_state()
    categories = read_categories()
    calendar   = read_calendar_state()

    # 초기 대시보드 갱신은 구조와 상태만 필요하다. 본문은 /api/phrase/:key에서 지연 로딩한다.
    coverage_summary = json.loads(json.dumps(coverage, ensure_ascii=False))
    for exam_data in coverage_summary.values():
        exam_data.pop('slotContents', None)

    # KPI 계산
    total_slots = sum(v.get('total', 0) for v in coverage.values())
    done_slots  = sum(v.get('done',  0) for v in coverage.values())
    coverage_pct = f'{round(done_slots / total_slots * 100) if total_slots else 0}%'

    return {
        'logs':         logs,
        'coverage':     coverage_summary,
        'activities':   activities,
        'tasks':        tasks,
        'calendar':     calendar,
        'meetings':     meetings.get('meetings', [])[:80],
        'meetingKpis':  meetings.get('kpis', {}),
        'meetingSync': {
            'updated_at': meetings.get('updated_at', ''),
            'sync_status': meetings.get('sync_status', ''),
            'sync_requested_at': meetings.get('sync_requested_at', ''),
        },
        'categories':   categories,
        'kpis': {
            'coverage_pct': coverage_pct,
            'log_count':    len(logs),
            'task_count':   len(tasks),
            'meeting_unprocessed': meetings.get('kpis', {}).get('unprocessed', 0),
        },
        'generated_at': datetime.now().isoformat(),
    }


def handle_action(action, data):
    if action == 'task':
        task, _ = save_task_request(data, start=True)
        return {'ok': True, 'task': task}
    if action == 'meetings/sync':
        return create_meeting_sync_task(data)
    if action == 'meetings/process':
        return create_meeting_process_task(data)
    if action == 'video/encoding/start':
        return create_video_encoding_task(data)
    if action == 'video/encoding/cancel':
        return cancel_video_encoding_task(data)
    if action == 'video/transcript-review':
        return create_video_transcript_review_task(data)
    if action == 'calendar':
        return save_calendar_event(data)
    if action == 'calendar/parse':
        return parse_calendar_request(data)
    if action == 'calendar/delete':
        return delete_calendar_event(data)
    return {'ok': False, 'error': 'Unknown action'}


# ── HTTP 핸들러 ──────────────────────────────────────────────────────

class CrataHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        ts = datetime.now().strftime('%H:%M:%S')
        print(f'[{ts}] {format % args}')

    def send_json(self, data, status=200, head_only=False):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        if head_only:
            return
        self.wfile.write(body)

    def send_json_or_jsonp(self, data, query, status=200):
        callback = (parse_qs(query).get('callback') or [''])[0]
        if re.match(r'^[A-Za-z_$][\w$]*$', callback):
            payload = json.dumps(data, ensure_ascii=False)
            body = f'{callback}({payload});'.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/javascript; charset=utf-8')
            self.send_header('Content-Length', len(body))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json(data, status)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Range')
        self.send_header('Access-Control-Expose-Headers', 'Accept-Ranges, Content-Length, Content-Range')
        self.end_headers()

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/api/video/preview':
            query = parse_qs(parsed.query)
            preview_path = (query.get('path') or [''])[0]
            self.stream_video_preview(preview_path, head_only=True)
        elif path in ('/api/video/status', '/video_status.json'):
            self.send_json(video_status_payload(), head_only=True)
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # --- API 라우팅 ---
        if path == '/api/data':
            self.send_json_or_jsonp(get_all_data(), parsed.query)

        elif path == '/api/coverage':
            self.send_json(read_coverage())

        elif path == '/api/logs':
            self.send_json(read_decisions_logs())

        elif path == '/api/categories':
            self.send_json_or_jsonp({'categories': read_categories()}, parsed.query)

        elif path == '/api/meetings':
            self.send_json_or_jsonp(read_meetings_state(), parsed.query)

        elif path == '/api/meetings/transcript':
            meeting_id = (parse_qs(parsed.query).get('id') or [''])[0]
            self.send_json_or_jsonp(read_meeting_transcript(meeting_id), parsed.query)

        elif path == '/api/calendar':
            self.send_json_or_jsonp(read_calendar_state(), parsed.query)

        elif path == '/api/video/browse':
            query = parse_qs(parsed.query)
            browse_path = (query.get('path') or [''])[0]
            self.send_json_or_jsonp(browse_video_files(browse_path), parsed.query)

        elif path == '/api/video/artifact':
            query = parse_qs(parsed.query)
            artifact_path = (query.get('path') or [''])[0]
            result = read_video_artifact(artifact_path)
            self.send_json_or_jsonp(result, parsed.query, 200 if result.get('ok') else 404)

        elif path == '/api/video/subtitle-style':
            self.send_json_or_jsonp({'ok': True, 'style': read_video_subtitle_style()}, parsed.query)

        elif path in ('/api/video/status', '/video_status.json'):
            self.send_json_or_jsonp(video_status_payload(), parsed.query)

        elif path == '/api/video/preview':
            query = parse_qs(parsed.query)
            preview_path = (query.get('path') or [''])[0]
            self.stream_video_preview(preview_path)

        elif path == '/api/video/edit':
            self.send_json_or_jsonp(read_video_edit_queue(), parsed.query)

        elif path.startswith('/api/action/'):
            payload = (parse_qs(parsed.query).get('payload') or ['{}'])[0]
            try:
                data = json.loads(payload)
            except Exception:
                data = {}
            action = path[len('/api/action/'):]
            self.send_json_or_jsonp(handle_action(action, data), parsed.query)

        elif path.startswith('/api/phrase/'):
            key      = path.split('/')[-1]
            coverage = read_coverage()
            for exam_data in coverage.values():
                sc = exam_data.get('slotContents', {})
                if key in sc:
                    self.send_json({'key': key, 'content': sc[key]})
                    return
            self.send_json({'key': key, 'content': ''})

        elif path == '/api/inbox':
            self.send_json(read_inbox_tasks())

        # --- 정적 파일 ---
        elif path == '/' or path == '':
            self._serve_file(ROOT / 'dashboard.html')
        else:
            self._serve_file(ROOT / path.lstrip('/'))

    def _serve_file(self, fpath):
        fpath = pathlib.Path(fpath)
        if fpath.is_file():
            mime, _ = mimetypes.guess_type(str(fpath))
            self.send_response(200)
            self.send_header('Content-Type', mime or 'application/octet-stream')
            self.end_headers()
            self.wfile.write(fpath.read_bytes())
        else:
            self.send_response(404)
            self.end_headers()

    def stream_video_preview(self, raw_path, head_only=False):
        fpath, error = resolve_video_preview_path(raw_path)
        if error:
            self.send_json({'ok': False, 'error': error}, 404, head_only=head_only)
            return

        file_size = fpath.stat().st_size
        range_header = self.headers.get('Range') or ''
        start = 0
        end = file_size - 1
        status = 200

        match = re.match(r'bytes=(\d*)-(\d*)', range_header)
        if match:
            status = 206
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{file_size}')
                self.end_headers()
                return

        length = end - start + 1
        mime, _ = mimetypes.guess_type(str(fpath))
        self.send_response(status)
        self.send_header('Content-Type', mime or 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        self.send_header('Content-Disposition', inline_content_disposition(fpath.name))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Expose-Headers', 'Accept-Ranges, Content-Length, Content-Range')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
        self.end_headers()

        if head_only:
            return

        try:
            with fpath.open('rb') as src:
                src.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        path = urlparse(self.path).path

        if path == '/api/video/upload':
            self.send_json(handle_video_upload(self))
            return

        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length) if length else b'{}'

        try:
            data = json.loads(body.decode('utf-8'))
        except Exception:
            data = {}

        inbox_dir = ROOT / '처리관리' / 'inbox'
        inbox_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')

        if path == '/api/task':
            task, _ = save_task_request(data, start=True)
            self.send_json({'ok': True, 'task': task})

        elif path == '/api/meetings/sync':
            self.send_json(create_meeting_sync_task(data))

        elif path == '/api/meetings/process':
            self.send_json(create_meeting_process_task(data))

        elif path == '/api/calendar':
            self.send_json(save_calendar_event(data))

        elif path == '/api/calendar/parse':
            self.send_json(parse_calendar_request(data))

        elif path == '/api/calendar/delete':
            self.send_json(delete_calendar_event(data))

        elif path == '/api/system/update':
            result = update_from_git()
            if result.get('ok') and result.get('updated') and result.get('server_restart_required'):
                try:
                    result['restart_scheduled'] = schedule_server_restart(self.server)
                except Exception as exc:
                    result['restart_scheduled'] = False
                    result['restart_error'] = str(exc)
            self.send_json(result)

        elif path == '/api/video/encoding/start':
            self.send_json(create_video_encoding_task(data))

        elif path == '/api/video/encoding/cancel':
            self.send_json(cancel_video_encoding_task(data))

        elif path == '/api/video/transcript-review':
            self.send_json(create_video_transcript_review_task(data))

        elif path == '/api/video/subtitle-style':
            self.send_json(save_video_subtitle_style(data))

        elif path == '/api/video/edit':
            self.send_json(save_video_edit_action(data))

        elif path == '/api/phrase':
            fpath = inbox_dir / f'phrase_{ts}.json'
            req   = {
                'type':       'phrase_edit',
                'created_at': datetime.now().isoformat(),
                **data,
            }
            fpath.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'  → 문구 수정 요청 저장: {fpath.name}')
            self.send_json({'ok': True})

        elif path == '/api/review_feedback':
            fpath = inbox_dir / f'review_{ts}.json'
            fb    = {
                'type':       'review_feedback',
                'created_at': datetime.now().isoformat(),
                **data,
            }
            fpath.write_text(json.dumps(fb, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'  → 검수 피드백 저장: {fpath.name}')
            self.send_json({'ok': True})

        else:
            self.send_json({'error': 'Unknown endpoint'}, 404)


if __name__ == '__main__':
    HOST, PORT = server_bind_config()
    read_coverage()
    server = ThreadingHTTPServer((HOST, PORT), CrataHandler)
    print(f'CRATA 대시보드 서버 시작됨')
    print(f'바인딩: {HOST}:{PORT}')
    print(f'로컬 접속: http://localhost:{PORT}')
    if HOST in ('0.0.0.0', ''):
        for addr in local_ipv4_addresses():
            print(f'같은 네트워크 접속: http://{addr}:{PORT}')
        print(f'Tailscale 접속: http://<데스크탑-Tailscale-IP>:{PORT}')
    print(f'종료: Ctrl+C\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n서버 종료.')
