import os
import re
import time
import queue
import logging
import threading
import uuid
import socket
import tempfile
import shutil
import contextlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote
from flask import Flask, render_template, request, jsonify
from patchright.sync_api import sync_playwright
import httpx
import psutil

app = Flask(__name__)
app.secret_key = os.urandom(24)
log = logging.getLogger("fitgrab")

# ─── Config ───
MAX_RETRIES = 5
RETRY_DELAYS = [2, 5, 10, 20, 60]
NUM_SEGMENTS = 8
CHUNK_SIZE = 2 * 1024 * 1024  # 2MB — fewer syscalls, better throughput
SEGMENT_MIN_FILE = 10 * 1024 * 1024  # 10MB
MERGE_BUFFER = 8 * 1024 * 1024
EXTRACT_AHEAD = 2  # direct links expire, so don't extract too far ahead of the downloader
LINK_EXPIRED_CODES = (403, 404, 410)
MAX_FINISHED_SESSIONS = 20


class Cancelled(Exception):
    pass


@contextlib.contextmanager
def browser_context(p):
    """Headed context in a private, throwaway profile dir — fuckingfast.co's
    Cloudflare bot-management blocks the download-trigger request under
    headless automation. A fresh dir per launch avoids two launches (e.g.
    link-fetch and a download session) racing on the same profile lock."""
    tmp_dir = tempfile.mkdtemp(prefix="fitgirl_pw_")
    context = p.chromium.launch_persistent_context(
        tmp_dir,
        headless=False,
        no_viewport=True,
    )
    try:
        yield context
    finally:
        context.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def install_ad_tab_closer(context, main_page):
    """Auto-close any tab the context opens other than main_page — covers
    ad tabs from the download-click flow and any pop-under chains they
    spawn. BrowserContext has no 'popup' event (only Page does), so we
    watch 'page' instead, which fires for every new tab regardless of
    nesting. Must be attached AFTER main_page exists: if attached before,
    main_page's own creation event can race ahead of registration and get
    itself closed."""
    def _close_unwanted(pg):
        if pg is not main_page:
            try:
                pg.close()
            except Exception:
                pass
    context.on("page", _close_unwanted)


# ─── Network Monitor ───
class NetworkMonitor:
    """Samples once a second. Tracks two rates: the whole machine's receive
    rate (psutil) and this app's own download rate (bytes reported by the
    download loops), so the graph shows our transfers, not background traffic."""
    DNS_ENDPOINTS = [("1.1.1.1", 53), ("8.8.8.8", 53), ("208.67.222.222", 53)]

    def __init__(self):
        self._lock = threading.Lock()
        self._started = False
        self._app_bytes = 0
        self.connected = True
        self.system_speed = 0
        self.app_speed = 0
        self.history = deque(maxlen=120)  # one sample per second → 2 minutes

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._sample_loop, daemon=True).start()
        threading.Thread(target=self._connectivity_loop, daemon=True).start()

    def add_bytes(self, n):
        with self._lock:
            self._app_bytes += n

    def _sample_loop(self):
        prev_t = time.monotonic()
        prev_sys = psutil.net_io_counters().bytes_recv
        prev_app = 0
        while True:
            time.sleep(1)
            try:
                now = time.monotonic()
                sys_recv = psutil.net_io_counters().bytes_recv
                dt = now - prev_t
                with self._lock:
                    app_bytes = self._app_bytes
                    # Counters can reset when an adapter goes away; clamp instead of going negative.
                    self.system_speed = max(0, sys_recv - prev_sys) / dt
                    self.app_speed = (app_bytes - prev_app) / dt
                    self.history.append(round(self.app_speed))
                prev_t, prev_sys, prev_app = now, sys_recv, app_bytes
            except Exception:
                log.exception("Network sampling failed")

    def _connectivity_loop(self):
        while True:
            self.connected = any(self._reachable(ep) for ep in self.DNS_ENDPOINTS)
            time.sleep(2)

    @staticmethod
    def _reachable(endpoint):
        try:
            with socket.create_connection(endpoint, timeout=1.5):
                return True
        except OSError:
            return False

    def snapshot(self):
        with self._lock:
            return {
                'connected': self.connected,
                'speed': round(self.system_speed),
                'speed_str': fmt_speed(self.system_speed),
                'app_speed': round(self.app_speed),
                'app_speed_str': fmt_speed(self.app_speed),
                'history': list(self.history),
            }


class SpeedMeter:
    """Rolling-window rate, so speed and ETA reflect the last few seconds
    rather than the average since the transfer started."""
    def __init__(self, window=5.0):
        self.window = window
        self.samples = deque()

    def update(self, total_bytes):
        now = time.monotonic()
        self.samples.append((now, total_bytes))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        t0, b0 = self.samples[0]
        return (total_bytes - b0) / (now - t0) if now > t0 else 0


net_monitor = NetworkMonitor()
app.before_request(net_monitor.start)
download_sessions = {}
fetch_cache = {}


# ─── Formatters ───
def fmt_speed(bps):
    if bps >= 1024**3: return f"{bps/1024**3:.1f} GB/s"
    if bps >= 1024**2: return f"{bps/1024**2:.1f} MB/s"
    if bps >= 1024: return f"{bps/1024:.1f} KB/s"
    return f"{bps:.0f} B/s"

def fmt_size(b):
    if b >= 1024**3: return f"{b/1024**3:.2f} GB"
    if b >= 1024**2: return f"{b/1024**2:.1f} MB"
    if b >= 1024: return f"{b/1024:.1f} KB"
    return f"{b} B"

def fmt_eta(secs):
    if secs <= 0 or secs > 86400: return "--:--"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                   *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

def safe_filename(name):
    """The filename comes from a link inside someone else's paste, so it's
    untrusted: drop any directory parts and characters Windows rejects so it
    can never point outside the download folder."""
    name = re.split(r'[\\/]', unquote(name))[-1]
    name = _INVALID_FILENAME_CHARS.sub('_', name).strip().rstrip('. ')
    if name.split('.')[0].upper() in _RESERVED_NAMES:
        name = '_' + name
    return name or "unknown_file"

def parse_filename(url):
    raw = url.split('#', 1)[1] if '#' in url else url.split('?')[0].rstrip('/').split('/')[-1]
    return safe_filename(raw)

def split_ranges(total, n):
    """Inclusive (start, end) byte ranges covering 0..total-1; the last one takes the remainder."""
    size = total // n
    return [(i * size, total - 1 if i == n - 1 else (i + 1) * size - 1) for i in range(n)]


# ─── Paste Link Extraction ───
def extract_links_from_paste(paste_url):
    links = []
    with sync_playwright() as p, browser_context(p) as context:
        page = context.new_page()
        install_ad_tab_closer(context, page)
        page.goto(paste_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)
        for a in page.query_selector_all("a"):
            href = a.get_attribute("href")
            if href and "fuckingfast.co" in href:
                links.append(href)
        body = page.inner_text("body")
        for link in re.findall(r'https?://fuckingfast\.co/\S+', body):
            links.append(link.rstrip('.,;:)'))
    return list(dict.fromkeys(links))


# ─── Direct URL Extraction ───
def extract_direct_url_page(browser_page, fuckingfast_url):
    """fuckingfast.co gates the real link behind a two-click ad flow:
    click 1 opens an ad tab, click 2 POSTs to /f/<id>/go which replies
    with an `HX-Redirect` header pointing at the real dl.fuckingfast.co URL
    (normally followed client-side by htmx; we just read the header)."""
    direct_url = None
    filesize = None
    go_result = {}

    def on_response(resp):
        if resp.request.method == "POST" and "/go" in resp.url:
            hx = resp.headers.get("hx-redirect")
            if hx:
                go_result['url'] = hx

    browser_page.on("response", on_response)
    try:
        browser_page.goto(fuckingfast_url, wait_until="domcontentloaded", timeout=30000)
        browser_page.wait_for_timeout(500)

        body = browser_page.inner_text("body")
        sm = re.search(r'Size:\s*([^\|]+)', body)
        if sm:
            filesize = sm.group(1).strip()

        btn = browser_page.query_selector("a.link-button")
        if btn:
            btn.click()  # 1st click: opens ad tab (auto-closed by popup handler)
            browser_page.wait_for_timeout(800)
            btn.click()  # 2nd click: triggers /go -> HX-Redirect header
            for _ in range(20):
                if 'url' in go_result:
                    break
                browser_page.wait_for_timeout(150)
            direct_url = go_result.get('url')
    finally:
        browser_page.remove_listener("response", on_response)
        # Backstop: the context's auto-closer usually catches ad tabs the
        # instant they open, but a tab created mid-navigation can occasionally
        # dodge that close() call. Sweep anything still left behind here.
        for pg in list(browser_page.context.pages):
            if pg is not browser_page:
                try:
                    pg.close()
                except Exception:
                    pass

    return direct_url, filesize


# ─── Download Engine ───
def _http_client(**kwargs):
    # Fresh client per call — old TCP connections are dead after a network switch
    return httpx.Client(
        timeout=httpx.Timeout(15, read=30, pool=10),
        follow_redirects=True,
        http2=True,
        **kwargs
    )


def _status_code(exc):
    return exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None


def _report(file_info, downloaded, total, speed):
    file_info.update({
        'downloaded_bytes': downloaded,
        'progress': round(downloaded / total * 100, 1) if total else 0,
        'speed': speed, 'speed_str': fmt_speed(speed),
        'eta': fmt_eta((total - downloaded) / speed if speed > 0 and total else 0),
    })


def _wait_for_network(file_info, session):
    """Block until network is back or cancelled. Returns False if cancelled."""
    if net_monitor.connected:
        return True
    file_info['status'] = 'paused_network'
    while not net_monitor.connected:
        if session.get('cancelled'):
            return False
        time.sleep(0.5)
    # Extra settle time for new network to stabilize
    time.sleep(1.5)
    return True


def _segment_paths(filepath, n=NUM_SEGMENTS):
    return [f"{filepath}.seg{i}" for i in range(n)]


def _already_complete(filepath, total):
    return (os.path.isfile(filepath) and os.path.getsize(filepath) == total
            and not any(os.path.exists(p) for p in _segment_paths(filepath)))


def _probe(url):
    """HEAD doesn't return content-length/accept-ranges on this host's CDN,
    but it honors Range on GET, so probe with a 1-byte ranged GET instead.
    Returns (total_size, supports_ranges)."""
    with httpx.Client(follow_redirects=True, timeout=15) as c:
        probe = c.get(url, headers={'Range': 'bytes=0-0'})
        probe.raise_for_status()
        if probe.status_code == 206:
            cr = probe.headers.get('content-range', '')
            return (int(cr.split('/')[-1]) if '/' in cr else 0), True
        return int(probe.headers.get('content-length', 0)), False


def _download_single_resume(url, filepath, file_info, session):
    """Single-stream download WITH resume support via Range headers."""
    existing = os.path.getsize(filepath) if os.path.exists(filepath) else 0
    headers = {'Range': f'bytes={existing}-'} if existing else {}

    with _http_client(limits=httpx.Limits(max_connections=5)) as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416:
                # Local file is longer than the remote one — it's not ours to resume.
                os.remove(filepath)
                raise RuntimeError("Partial file didn't match the server copy; restarting")
            resp.raise_for_status()
            if resp.status_code == 206:
                # Resuming — content-range tells us total size (bytes START-END/TOTAL)
                cr = resp.headers.get('content-range', '')
                total = int(cr.split('/')[-1]) if '/' in cr else 0
                downloaded = existing
                mode = "ab"
            else:
                # Server ignored the range and sent the full file
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                mode = "wb"

            file_info['total_bytes'] = total
            meter = SpeedMeter()
            with open(filepath, mode) as f:
                for chunk in resp.iter_bytes(CHUNK_SIZE):
                    if session.get('cancelled'):
                        raise Cancelled()
                    f.write(chunk)
                    downloaded += len(chunk)
                    net_monitor.add_bytes(len(chunk))
                    _report(file_info, downloaded, total, meter.update(downloaded))


def _download_segment_resume(url, temp_path, start, end, progress, seg_id, should_stop):
    """Download one byte-range segment with resume support."""
    expected = end - start + 1
    existing = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
    if existing > expected:  # leftover from a different file — start over
        os.remove(temp_path)
        existing = 0
    progress[seg_id] = existing
    if existing == expected:
        return

    headers = {'Range': f'bytes={start + existing}-{end}'}
    with _http_client() as client, client.stream("GET", url, headers=headers) as resp:
        resp.raise_for_status()
        if resp.status_code != 206:
            # A 200 is the whole file, not this segment; writing it would corrupt the output.
            raise RuntimeError("Server ignored the byte-range request")
        remaining = expected - existing
        with open(temp_path, "ab") as f:
            for chunk in resp.iter_bytes(CHUNK_SIZE):
                if should_stop():
                    raise Cancelled()
                chunk = chunk[:remaining]
                f.write(chunk)
                remaining -= len(chunk)
                progress[seg_id] += len(chunk)
                net_monitor.add_bytes(len(chunk))
                if remaining <= 0:
                    break
    if progress[seg_id] != expected:
        raise RuntimeError(f"Segment {seg_id} ended early")


def _download_multiseg(url, filepath, total, file_info, session, n=NUM_SEGMENTS):
    """Parallel multi-segment download with per-segment resume."""
    ranges = split_ranges(total, n)
    seg_paths = _segment_paths(filepath, n)
    progress = [0] * n
    meter = SpeedMeter()
    failed = threading.Event()
    should_stop = lambda: session.get('cancelled') or failed.is_set()
    file_info.update(total_bytes=total, segments_total=n, segments_done=0)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [
            pool.submit(_download_segment_resume, url, seg_paths[i], s, e, progress, i, should_stop)
            for i, (s, e) in enumerate(ranges)
        ]
        while not all(f.done() for f in futures):
            if session.get('cancelled'):
                break  # segments see the flag and stop on their next chunk
            if any(f.done() and f.exception() for f in futures):
                failed.set()  # one segment died; stop the rest and retry the file
                break
            dl = sum(progress)
            _report(file_info, dl, total, meter.update(dl))
            file_info['segments_done'] = sum(f.done() for f in futures)
            time.sleep(0.3)

    errors = [f.exception() for f in futures]
    real_error = next((e for e in errors if e and not isinstance(e, Cancelled)), None)
    if real_error:
        raise real_error
    if session.get('cancelled'):
        raise Cancelled()
    file_info['segments_done'] = n

    # Merge — rename first segment as output, append rest (saves copying first segment)
    file_info['status'] = 'merging'
    os.replace(seg_paths[0], filepath)
    with open(filepath, 'ab') as out:
        for tp in seg_paths[1:]:
            with open(tp, 'rb') as seg:
                shutil.copyfileobj(seg, out, MERGE_BUFFER)
            os.remove(tp)


def download_file(file_info, filepath, session, refresh_url):
    """Download with retries, resume, multi-segment, and network awareness.
    refresh_url() re-extracts the direct link and returns it (or None)."""
    attempt = 0
    while attempt < MAX_RETRIES:
        if not _wait_for_network(file_info, session):
            file_info['status'] = 'cancelled'
            return
        attempt += 1
        file_info.update(status='downloading', attempt=attempt, error='')
        try:
            url = file_info['direct_url']
            total, ranges_ok = _probe(url)
            file_info['total_bytes'] = total

            if total and _already_complete(filepath, total):
                file_info.update(status='completed', progress=100, downloaded_bytes=total,
                                 eta='0:00', speed_str='', note='Already downloaded')
                return

            if ranges_ok and total > SEGMENT_MIN_FILE:
                _download_multiseg(url, filepath, total, file_info, session)
            else:
                _download_single_resume(url, filepath, file_info, session)

            file_info.update(status='completed', progress=100, eta='0:00', speed=0, speed_str='')
            return

        except Exception as e:
            # Partial files and segments are kept on purpose — the next attempt resumes from them.
            if isinstance(e, Cancelled) or session.get('cancelled'):
                file_info['status'] = 'cancelled'
                return

            if not net_monitor.connected:
                # Network dropped mid-transfer: wait it out without burning an attempt.
                log.info("Network lost during %s; waiting", file_info['filename'])
                attempt -= 1
                continue

            if _status_code(e) in LINK_EXPIRED_CODES:
                log.info("Link for %s returned %s; re-extracting", file_info['filename'], _status_code(e))
                file_info['status'] = 'reextracting'
                if not refresh_url():
                    if not session.get('cancelled'):
                        file_info.update(status='error', error='Download link expired and could not be refreshed')
                    return
                continue

            if attempt < MAX_RETRIES:
                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                file_info.update(status='retrying', error=f"{e} (resuming in {delay}s)", retry_in=delay)
                time.sleep(delay)
            else:
                file_info.update(status='error', error=f"Failed after {MAX_RETRIES} attempts: {e}")


def download_worker(session_id, indices):
    """Process the download queue. URL extraction runs on its own browser/thread
    a couple of files ahead, so a slow Cloudflare-gated extraction rarely stalls
    the downloader, but links don't sit around long enough to expire. Playwright's
    sync API is bound to the thread that created it, so re-extraction requests
    for expired links go through the same queue."""
    session = download_sessions[session_id]
    files = session['files']
    download_dir = session['download_dir']
    log.info("Session %s: %d files -> %s", session_id, len(indices), download_dir)

    jobs = queue.Queue()
    requested = set()
    extractor_done = threading.Event()

    def request_extract(idx, force=False):
        if force or idx not in requested:
            requested.add(idx)
            jobs.put(idx)

    def extract_one(page, fi):
        fi['status'] = 'extracting'
        try:
            direct_url, filesize = extract_direct_url_page(page, fi['url'])
        except Exception as e:
            log.warning("Extract failed for %s: %s", fi['filename'], e)
            fi.update(status='error', error=str(e))
            return
        if direct_url:
            fi.update(direct_url=direct_url, status='queued')
            if filesize:
                fi['filesize_display'] = filesize
            log.info("Extracted %s", fi['filename'])
        else:
            fi.update(status='error', error='Could not extract download URL')
            log.warning("No download URL found for %s", fi['filename'])

    def extractor_loop():
        try:
            with sync_playwright() as p, browser_context(p) as context:
                page = context.new_page()
                install_ad_tab_closer(context, page)
                while not session.get('cancelled'):
                    try:
                        idx = jobs.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if idx is None:
                        break
                    extract_one(page, files[idx])
        except Exception as e:
            log.exception("Extractor browser failed")
            for idx in indices:
                fi = files[idx]
                if not fi.get('direct_url') and fi['status'] not in ('error', 'completed'):
                    fi.update(status='error', error=f'Browser launch failed: {e}')
        finally:
            extractor_done.set()

    def wait_for_url(fi):
        while not (session.get('cancelled') or fi.get('direct_url')
                   or fi['status'] == 'error' or extractor_done.is_set()):
            time.sleep(0.2)
        return fi.get('direct_url')

    extractor_thread = threading.Thread(target=extractor_loop, daemon=True)
    extractor_thread.start()

    for pos, idx in enumerate(indices):
        for upcoming in indices[pos:pos + EXTRACT_AHEAD + 1]:
            request_extract(upcoming)
        if session.get('cancelled'):
            break
        fi = files[idx]
        if not wait_for_url(fi):
            if session.get('cancelled'):
                break
            if fi['status'] != 'error':
                fi.update(status='error', error='Could not extract download URL')
            continue

        def refresh_url(fi=fi, idx=idx):
            fi['direct_url'] = None
            request_extract(idx, force=True)
            return wait_for_url(fi)

        log.info("Downloading %s", fi['filename'])
        try:
            download_file(fi, os.path.join(download_dir, fi['filename']), session, refresh_url)
            log.info("Finished %s -> %s", fi['filename'], fi['status'])
        except Exception as e:
            log.exception("Download failed for %s", fi['filename'])
            fi.update(status='error', error=str(e))

    jobs.put(None)
    extractor_thread.join(timeout=10)
    if session.get('cancelled'):
        for idx in indices:
            if files[idx]['status'] not in ('completed', 'error'):
                files[idx]['status'] = 'cancelled'
        session['overall_status'] = 'cancelled'
    else:
        session['overall_status'] = 'finished'
    log.info("Session %s %s", session_id, session['overall_status'])


def _start_worker(sid, indices):
    threading.Thread(target=download_worker, args=(sid, indices), daemon=True).start()


def _prune_sessions():
    finished = [sid for sid, s in download_sessions.items() if s['overall_status'] != 'running']
    for sid in finished[:-MAX_FINISHED_SESSIONS]:
        del download_sessions[sid]


def _summary(files):
    selected = [f for f in files if f['selected']]
    count = lambda status: sum(1 for f in selected if f['status'] == status)
    return {'total': len(selected), 'completed': count('completed'),
            'failed': count('error'), 'cancelled': count('cancelled')}


# ─── Routes ───
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/fetch-links', methods=['POST'])
def api_fetch_links():
    data = request.get_json(silent=True) or {}
    paste_url = str(data.get('paste_url', '')).strip()
    if not paste_url:
        return jsonify(success=False, error="No URL provided")
    if not paste_url.startswith(('http://', 'https://')):
        return jsonify(success=False, error="That doesn't look like a link")
    if paste_url in fetch_cache:
        return jsonify(success=True, files=fetch_cache[paste_url])
    try:
        links = extract_links_from_paste(paste_url)
        if not links:
            return jsonify(success=False, error="No download links found")
        files = [{"index": i, "url": l, "filename": parse_filename(l)} for i, l in enumerate(links)]
        fetch_cache[paste_url] = files
        return jsonify(success=True, files=files)
    except Exception as e:
        log.exception("Fetching paste failed")
        return jsonify(success=False, error=str(e))


@app.route('/api/start-download', methods=['POST'])
def api_start():
    data = request.get_json(silent=True) or {}
    selected = {i for i in data.get('selected', []) if isinstance(i, int)}
    dl_dir = os.path.expanduser(str(data.get('download_dir', '')).strip())
    paste_url = str(data.get('paste_url', '')).strip()
    if not dl_dir: return jsonify(success=False, error="No download directory")
    if not os.path.isabs(dl_dir): return jsonify(success=False, error="Use a full folder path, e.g. D:\\Games\\Name")
    if not selected: return jsonify(success=False, error="No files selected")
    if paste_url not in fetch_cache: return jsonify(success=False, error="Fetch links first")
    try:
        os.makedirs(dl_dir, exist_ok=True)
    except OSError as e:
        return jsonify(success=False, error=f"Can't use that folder: {e.strerror}")

    sid = str(uuid.uuid4())[:8]
    files = []
    for f in fetch_cache[paste_url]:
        files.append({
            "url": f["url"], "filename": f["filename"],
            "selected": f["index"] in selected,
            "status": "queued", "progress": 0,
            "downloaded_bytes": 0, "total_bytes": 0,
            "speed": 0, "speed_str": "", "eta": "--:--",
            "error": "", "note": "", "attempt": 0,
            "segments_done": 0, "segments_total": 0
        })
    download_sessions[sid] = {
        "files": files, "download_dir": dl_dir,
        "overall_status": "running", "cancelled": False
    }
    _prune_sessions()
    _start_worker(sid, [i for i, f in enumerate(files) if f['selected']])
    return jsonify(success=True, session_id=sid)


@app.route('/api/progress/<sid>')
def api_progress(sid):
    if sid not in download_sessions:
        return jsonify(success=False, error="Session not found")
    s = download_sessions[sid]
    return jsonify(
        success=True,
        overall_status=s['overall_status'],
        download_dir=s['download_dir'],
        max_attempts=MAX_RETRIES,
        summary=_summary(s['files']),
        files=[{
            'index': i,
            'filename': f['filename'],
            'status': f['status'], 'progress': f['progress'],
            'downloaded_bytes': f['downloaded_bytes'],
            'total_bytes': f['total_bytes'],
            'speed_str': f.get('speed_str', ''),
            'eta': f.get('eta', '--:--'),
            'error': f.get('error', ''),
            'note': f.get('note', ''),
            'attempt': f.get('attempt', 0),
            'segments_done': f.get('segments_done', 0),
            'segments_total': f.get('segments_total', 0),
            'downloaded_display': fmt_size(f['downloaded_bytes']),
            'total_display': fmt_size(f['total_bytes']) if f['total_bytes'] else '?'
        } for i, f in enumerate(s['files']) if f['selected']]
    )


@app.route('/api/network')
def api_network():
    return jsonify(net_monitor.snapshot())


@app.route('/api/cancel/<sid>', methods=['POST'])
def api_cancel(sid):
    if sid in download_sessions:
        download_sessions[sid]['cancelled'] = True
        return jsonify(success=True)
    return jsonify(success=False, error="Session not found")


@app.route('/api/retry/<sid>', methods=['POST'])
def api_retry(sid):
    """Re-run the failed and cancelled files of a finished session. Partial
    files on disk are resumed, not restarted."""
    s = download_sessions.get(sid)
    if not s:
        return jsonify(success=False, error="Session not found")
    if s['overall_status'] == 'running':
        return jsonify(success=False, error="Session is still running")
    indices = [i for i, f in enumerate(s['files'])
               if f['selected'] and f['status'] in ('error', 'cancelled')]
    if not indices:
        return jsonify(success=False, error="Nothing to retry")
    for i in indices:
        # Drop the old direct link; it has probably expired.
        s['files'][i].update(status='queued', error='', note='', attempt=0, direct_url=None,
                             speed_str='', eta='--:--', segments_done=0, segments_total=0)
    s.update(cancelled=False, overall_status='running')
    _start_worker(sid, indices)
    return jsonify(success=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # Localhost only: this server writes files and launches browsers on request,
    # so it must not be reachable from the rest of the network.
    app.run(host='127.0.0.1', port=5000, debug=os.environ.get('FITGRAB_DEBUG') == '1')
