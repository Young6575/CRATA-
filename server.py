#!/usr/bin/env python3
"""CRATA Dashboard Local Server
실행: python server.py
브라우저: http://localhost:8765
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json, re, glob, pathlib, mimetypes, os, shutil, socket, subprocess, tempfile, threading, time, uuid
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


def update_from_git():
    if not (ROOT / '.git').exists():
        return {'ok': False, 'error': '이 폴더는 Git 저장소가 아닙니다.'}

    code, status_out, status_err = run_git_command(['status', '--porcelain', '--untracked-files=no'], timeout=30)
    if code != 0:
        return {'ok': False, 'error': status_err or status_out or 'Git 상태 확인에 실패했습니다.'}

    _, before, _ = run_git_command(['rev-parse', 'HEAD'], timeout=30)
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


def read_video_status():
    data = read_json_file(VIDEO_STATUS_FILE, {})
    return data if isinstance(data, dict) else {}


def video_status_payload():
    status = {
        'status': 'idle',
        'progress': 0,
        'current_file': '',
        'batch_progress': 0,
        'total_files': 0,
        'completed_files': [],
        'current_step': 0,
        'updated_at': '',
    }
    status.update(read_video_status())
    return status


def inline_content_disposition(filename):
    raw = str(filename or '')
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', pathlib.PurePath(raw).stem).strip('._-') or 'preview'
    suffix = re.sub(r'[^A-Za-z0-9.]+', '', pathlib.PurePath(raw).suffix)[:16]
    fallback = f'{stem}{suffix}' if suffix else stem
    encoded = quote(str(filename or fallback), safe='')
    return f'inline; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


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
    text = f"{task.get('title', '')}\n{task.get('desc', '')}"
    if re.search(r'금지|하지\s*말|하지마|제외|skip|스킵', text, re.IGNORECASE):
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
        return

    update_task_file(fpath, lambda t: (
        t.update({'pid': proc.pid, 'progress': max(int(t.get('progress') or 0), 65)}),
        append_task_log(t, '시스템', f'Codex 프로세스 PID {proc.pid}로 실행 중입니다.')
    ))

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


def run_codex_fallback_task(fpath, claude_error='', claude_output=''):
    run_codex_task(fpath, claude_error=claude_error, claude_output=claude_output, fallback=True)


def run_claude_task(fpath):
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

    if not task_requests_plaud(task):
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

    return {'ok': False, 'id': meeting_id, 'error': 'transcript not found', 'transcript': ''}


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
        lines.append(f"- 목록 상태: {after.get('sync_status') or 'unknown'}")

        task['syncResult'] = {
            'before': before,
            'after': after,
            'newCount': len(new_ids),
            'removedCount': len(removed_ids),
            'newTitles': new_titles,
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
    runner_preference = data.get('runnerPreference') if data.get('runnerPreference') in ('claude', 'codex') else 'claude'
    before_snapshot = meeting_snapshot()
    cache = read_json_file(MEETINGS_FILE, {'meetings': []})
    cache['sync_status'] = 'requested'
    cache['sync_requested_at'] = now_iso()
    cache.setdefault('meetings', [])
    MEETINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(MEETINGS_FILE, cache)

    desc = """PLAUD 회의록 목록 업데이트 작업입니다.

해야 할 일:
1. Plaud MCP의 list_files로 최근 녹음 목록을 가져오세요.
2. 처리관리/녹음/plaud_processed.json의 processed[].id와 대조해 처리 완료/미처리를 구분하세요.
3. 처리관리/녹음/plaud_meetings.json을 갱신하세요. 구조는 { "updated_at": ISO시간, "sync_status": "done", "meetings": [{ "id", "title", "recorded_at", "created_at", "duration", "status" }] } 입니다.
4. 이 단계에서는 전사록 전체를 읽지 말고 목록만 업데이트하세요.
5. 오류가 있으면 sync_status를 "error"로 두고 대시보드 결과에 원인을 간단히 남기세요.
"""
    task, _ = save_task_request({
        'title': 'PLAUD 회의록 목록 업데이트',
        'desc': desc,
        'type': 'plaud_sync',
        'priority': 'normal',
        'priorityLbl': '보통',
        'agent': 'plan',
        'agentName': '기획담당',
        'runnerPreference': runner_preference,
        'categoryId': 'meeting-minutes',
        'categoryName': '회의록 처리',
        'meetingSyncBefore': before_snapshot,
        'assignments': [
            {'name': '기획담당', 'role': 'PLAUD 목록 동기화', 'progress': 0, 'status': 'pending'},
        ],
    }, start=True)
    return {'ok': True, 'task': task, 'meetings': read_meetings_state()}


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
        'preset': preset,
        'preset_label': preset_label,
        'steps': step_labels,
        'speaker_count': speaker_count,
        'review_required': True,
        'preview_required': needs_preview,
        'preview_confirmation_required': needs_preview,
        'review_order': ['raw_transcribe', 'diarize', 'transcript_quality_review', 'crata_term_correction', 'speaker_review', 'subtitle_preview_review', 'final_encode'],
    }
    write_json_file(VIDEO_STATUS_FILE, status)

    desc = f"""데스크탑 영상 인코딩 작업입니다.

중요:
- 이 작업은 시뮬레이션이 아닙니다. 실제 처리 가능한 로컬 파이프라인을 찾아 실행하세요.
- 우선 repo 안의 .agents/skills/video-encoding/SKILL.md를 읽고 그 절차를 따르세요.
- 실행 스크립트는 tools/video_agent/subtitle_agent.py를 우선 사용하세요. 아직 이관 전이면 C:\\Users\\wnsdu\\Desktop\\프로젝트\\영상편집에이전트\\subtitle_agent.py를 확인하세요.
- ffmpeg, faster-whisper large-v3, pyannote diarization, subtitle burn-in, final encode 관련 실행 가능성을 확인하세요.
- 실행 가능한 파이프라인이 없으면 임의 완료 처리하지 말고 필요한 스크립트/의존성/명령을 결과에 명확히 보고하세요.
- 진행 중에는 video_status.json을 갱신하세요. 형식은 status(active|requested|done|error), progress, current_file, batch_progress, total_files, completed_files, current_step, message, speaker_count 입니다.
- MXF는 웹 미리보기가 안 될 수 있지만 ffmpeg 입력으로는 처리 가능할 수 있습니다.

소스 경로:
{source_path}

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

필수 품질검토 순서:
1. 원본 전사: faster-whisper large-v3로 raw SRT/전사본을 생성하세요. 이 원본은 보존합니다.
2. 화자분리: 입력된 화자 수({speaker_count_label})를 기준으로 raw 전사 세그먼트에 화자 라벨을 붙이세요.
3. 전사 품질검토: 화자 라벨이 붙은 전사록 전체를 문맥 기준으로 읽고 오인식, 끊김, 반복, 어색한 문장을 검토하세요.
4. CRATA 용어 교정: 지식/과 결과지문구/의 공식 용어를 기준으로 CRATA 관련 단어를 교정하세요. 의미가 바뀔 수 있는 부분은 임의 확정하지 말고 확인 필요로 남기세요.
5. 화자분리 검토: 강사/질문자 라벨이 문맥상 뒤바뀐 구간, 짧은 맞장구, 질문 구간을 확인해 수정하거나 확인 필요로 표시하세요.
6. 자막 미리보기 검수: 검토 완료된 전사/ASS로 30~60초 미리보기 클립을 먼저 생성하세요. 자막 크기, 위치, 하단 여백, 줄 수, 화자 색상, 얼굴/자료 화면 가림 여부를 확인할 수 있어야 합니다.
7. 미리보기 승인 대기: 미리보기 파일 경로를 남기고 video_status.json에 status를 waiting_preview_review, preview_file, message로 갱신하세요. 사용자가 확인하기 전에는 최종 인코딩을 진행하지 마세요.
8. 사용자가 미리보기 확인 후 승인한 경우에만 검토 완료된 전사/ASS를 기준으로 자막 하드코딩과 최종 인코딩을 진행하세요.

처리 지침:
1. 소스 경로가 데스크탑 서버 기준 실제 경로인지 다시 확인하세요.
2. 선택된 단계만 수행하되, 전사 또는 화자분리를 수행했다면 품질검토와 CRATA 용어 교정은 건너뛰지 마세요.
3. 자막 하드코딩 또는 최종 인코딩 단계가 선택되어 있다면 미리보기 검수는 필수입니다.
4. 현재 작업 안에서 사용자 승인을 받을 수 없으면 미리보기 생성 후 확인 대기 상태로 멈추고, 최종 인코딩은 다음 승인 작업에서 진행하세요.
5. 결과 파일 경로와 실패한 파일이 있으면 실패 원인을 남기세요.
6. 완료 후 편집이 필요한 결과물은 처리관리/video_edit_queue.json에 pending_edit 항목으로 추가하세요.
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
        'videoFileCount': len(files),
        'videoSteps': step_labels,
        'videoPreset': preset,
        'videoPresetLabel': preset_label,
        'videoSpeakerCount': speaker_count,
        'videoReviewRequired': True,
        'videoPreviewRequired': needs_preview,
        'assignments': [
            {'name': '영상담당', 'role': '영상 인코딩 파이프라인 실행', 'progress': 0, 'status': 'pending'},
        ],
    }, start=True)
    return {'ok': True, 'task': task, 'videoStatus': status}


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
