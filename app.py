import os
import re
import time
import threading
import uuid
import socket
import tempfile
import shutil
import contextlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify
from patchright.sync_api import sync_playwright
import httpx
import psutil

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ─── Config ───
MAX_RETRIES = 5
RETRY_DELAYS = [2, 5, 10, 20, 60]
NUM_SEGMENTS = 8
CHUNK_SIZE = 2 * 1024 * 1024  # 2MB — fewer syscalls, better throughput
SEGMENT_MIN_FILE = 10 * 1024 * 1024  # 10MB
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
    def __init__(self):
        self._lock = threading.Lock()
        c = psutil.net_io_counters()
        self._prev_recv = c.bytes_recv
        self._prev_time = time.time()
        self.download_speed = 0
        self.history = deque(maxlen=120)
        self.connected = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        dns_endpoints = [("1.1.1.1", 53), ("8.8.8.8", 53), ("208.67.222.222", 53)]
        while True:
            try:
                now = time.time()
                c = psutil.net_io_counters()
                elapsed = now - self._prev_time
                with self._lock:
                    self.download_speed = (c.bytes_recv - self._prev_recv) / elapsed if elapsed > 0 else 0
                    self._prev_recv = c.bytes_recv
                    self._prev_time = now
                    self.history.append({'t': round(now, 1), 's': round(self.download_speed)})
                # Try multiple endpoints — any success = connected
                connected = False
                for ep in dns_endpoints:
                    try:
                        socket.create_connection(ep, timeout=1.5)
                        connected = True
                        break
                    except OSError:
                        continue
                self.connected = connected
            except Exception:
                pass
            time.sleep(0.5)

    def snapshot(self):
        with self._lock:
            return {
                'connected': self.connected,
                'speed': round(self.download_speed),
                'speed_str': fmt_speed(self.download_speed),
                'history': list(self.history)
            }


net_monitor = NetworkMonitor()
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

def parse_filename(url):
    if '#' in url: return url.split('#')[1]
    return url.split('/')[-1] or "unknown_file"


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
            link = link.rstrip('.,;:)')
            if link not in links:
                links.append(link)
    return links


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
def _wait_for_network(file_info, session):
    """Block until network is back or cancelled. Fast polling."""
    if net_monitor.connected:
        return True
    file_info['status'] = 'paused_network'
    while not net_monitor.connected:
        if session.get('cancelled'):
            return False
        time.sleep(0.5)
    # Extra settle time for new network to stabilize
    time.sleep(1.5)
    file_info['status'] = 'downloading'
    return True


def _download_single_resume(url, filepath, file_info, session):
    """Single-stream download WITH resume support via Range headers."""
    # Check how much we already have
    existing = 0
    if os.path.exists(filepath):
        existing = os.path.getsize(filepath)

    headers = {}
    if existing > 0:
        headers['Range'] = f'bytes={existing}-'

    # Fresh client each call — old TCP connections are dead after network switch
    with httpx.Client(
        timeout=httpx.Timeout(15, read=30, pool=10),
        follow_redirects=True,
        http2=True,
        limits=httpx.Limits(max_connections=5)
    ) as client:
        with client.stream("GET", url, headers=headers) as resp:
            # If server supports resume: 206 Partial Content
            # If not: 200 OK (full file, restart)
            if resp.status_code == 206:
                # Resuming — content-range tells us total size
                cr = resp.headers.get('content-range', '')
                # format: bytes START-END/TOTAL
                total = int(cr.split('/')[-1]) if '/' in cr else 0
                downloaded = existing
                mode = "ab"  # append
            elif resp.status_code == 200:
                # Server doesn't support resume or sent full file
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                existing = 0
                mode = "wb"  # overwrite
            else:
                resp.raise_for_status()
                return

            file_info['total_bytes'] = total
            file_info['downloaded_bytes'] = downloaded
            t0 = time.time()

            with open(filepath, mode) as f:
                for chunk in resp.iter_bytes(CHUNK_SIZE):
                    if session.get('cancelled'):
                        raise Exception("Cancelled")
                    f.write(chunk)
                    downloaded += len(chunk)
                    elapsed = time.time() - t0
                    # Speed is based on THIS session's transfer, not total
                    session_bytes = downloaded - existing
                    speed = session_bytes / elapsed if elapsed > 0 else 0
                    file_info.update({
                        'downloaded_bytes': downloaded,
                        'progress': round(downloaded / total * 100, 1) if total else 0,
                        'speed': speed, 'speed_str': fmt_speed(speed),
                        'eta': fmt_eta((total - downloaded) / speed if speed > 0 else 0)
                    })


def _download_segment_resume(url, temp_path, start, end, progress_list, seg_id, session):
    """Download one byte-range segment with resume support."""
    # Check existing progress
    existing = 0
    if os.path.exists(temp_path):
        existing = os.path.getsize(temp_path)

    actual_start = start + existing
    if actual_start > end:
        # Segment already complete
        progress_list[seg_id] = end - start + 1
        return

    progress_list[seg_id] = existing

    headers = {'Range': f'bytes={actual_start}-{end}'}
    with httpx.Client(
        timeout=httpx.Timeout(15, read=30, pool=10),
        follow_redirects=True,
        http2=True
    ) as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code not in (200, 206):
                resp.raise_for_status()
            mode = "ab" if existing > 0 else "wb"
            with open(temp_path, mode) as f:
                for chunk in resp.iter_bytes(CHUNK_SIZE):
                    if session.get('cancelled'):
                        raise Exception("Cancelled")
                    f.write(chunk)
                    progress_list[seg_id] += len(chunk)


def _download_multiseg(url, filepath, total, file_info, session, n=NUM_SEGMENTS):
    """Parallel multi-segment download with per-segment resume."""
    seg_size = total // n
    segments = []
    for i in range(n):
        s = i * seg_size
        e = total - 1 if i == n - 1 else (i + 1) * seg_size - 1
        segments.append((s, e, f"{filepath}.seg{i}", i))

    progress = [0] * n
    file_info['total_bytes'] = total
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {
            pool.submit(_download_segment_resume, url, tp, s, e, progress, i, session): i
            for s, e, tp, i in segments
        }
        while not all(f.done() for f in futures):
            if session.get('cancelled'):
                for f in futures:
                    f.cancel()
                raise Exception("Cancelled")
            dl = sum(progress)
            elapsed = time.time() - t0
            speed = dl / elapsed if elapsed > 0 else 0
            file_info.update({
                'downloaded_bytes': dl,
                'progress': round(dl / total * 100, 1),
                'speed': speed, 'speed_str': fmt_speed(speed),
                'eta': fmt_eta((total - dl) / speed if speed > 0 else 0),
                'segments_done': sum(1 for f in futures if f.done()),
                'segments_total': n
            })
            time.sleep(0.3)

        for f in futures:
            f.result()  # raises if segment failed

    # Merge — rename first segment as output, append rest (saves copying first segment)
    file_info['status'] = 'merging'
    first_seg_path = segments[0][2]
    os.replace(first_seg_path, filepath)  # instant rename, no copy
    if len(segments) > 1:
        with open(filepath, 'ab') as out:  # append mode
            for _, _, tp, _ in segments[1:]:
                with open(tp, 'rb') as seg:
                    while True:
                        chunk = seg.read(8 * 1024 * 1024)  # 8MB merge buffer
                        if not chunk:
                            break
                        out.write(chunk)
                os.remove(tp)


def _cleanup_segments(filepath):
    for i in range(NUM_SEGMENTS):
        tp = f"{filepath}.seg{i}"
        if os.path.exists(tp):
            try: os.remove(tp)
            except: pass


def download_file(url, filepath, file_info, session):
    """Download with retries, resume, multi-segment, and network awareness."""
    for attempt in range(MAX_RETRIES):
        try:
            file_info['status'] = 'downloading'
            file_info['attempt'] = attempt + 1
            file_info['error'] = ''

            # Probe file with fresh client. HEAD doesn't return content-length/
            # accept-ranges on this host's CDN, but it honors Range on GET, so
            # probe with a 1-byte ranged GET instead.
            with httpx.Client(follow_redirects=True, timeout=15) as c:
                probe = c.get(url, headers={'Range': 'bytes=0-0'})
                if probe.status_code == 206:
                    cr = probe.headers.get('content-range', '')
                    total = int(cr.split('/')[-1]) if '/' in cr else 0
                    ranges_ok = True
                else:
                    total = int(probe.headers.get('content-length', 0))
                    ranges_ok = False

            file_info['total_bytes'] = total

            if ranges_ok and total > SEGMENT_MIN_FILE:
                _download_multiseg(url, filepath, total, file_info, session)
            else:
                # Use resume-capable single download
                _download_single_resume(url, filepath, file_info, session)

            file_info['status'] = 'completed'
            file_info['progress'] = 100
            file_info['eta'] = '0:00'
            return

        except Exception as e:
            err_str = str(e)

            if session.get('cancelled') or 'Cancelled' in err_str:
                file_info['status'] = 'cancelled'
                return

            # DON'T delete partial file — we resume from it!
            # Only clean segments if multi-seg failed catastrophically
            if 'Cancelled' in err_str:
                _cleanup_segments(filepath)

            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                file_info['status'] = 'retrying'
                file_info['error'] = f"{e} (resuming in {delay}s)"
                file_info['retry_in'] = delay
                time.sleep(delay)
            else:
                file_info['status'] = 'error'
                file_info['error'] = f"Failed after {MAX_RETRIES} attempts: {e}"


def download_worker(session_id):
    """Process the download queue. URL extraction runs on its own browser/thread,
    working ahead continuously, so a slow Cloudflare-gated extraction never
    stalls a download that's already able to proceed."""
    session = download_sessions[session_id]
    download_dir = session['download_dir']
    os.makedirs(download_dir, exist_ok=True)
    print(f"[WORKER] Starting download worker {session_id}, dir={download_dir}")

    selected_indices = [i for i, fi in enumerate(session['files']) if fi.get('selected')]

    def extractor_loop():
        try:
            print("[WORKER] Launching extractor browser...")
            with sync_playwright() as p, browser_context(p) as context:
                page = context.new_page()
                install_ad_tab_closer(context, page)
                print("[WORKER] Extractor browser launched OK")
                for idx in selected_indices:
                    if session.get('cancelled'):
                        break
                    fi = session['files'][idx]
                    fi['status'] = 'extracting'
                    print(f"[WORKER] Extracting URL for: {fi['filename']}")
                    try:
                        direct_url, filesize = extract_direct_url_page(page, fi['url'])
                        if direct_url:
                            fi['direct_url'] = direct_url
                            if filesize:
                                fi['filesize_display'] = filesize
                            fi['status'] = 'queued'  # ready to download
                            print(f"[WORKER] Extracted: {fi['filename']}")
                        else:
                            fi['status'] = 'error'
                            fi['error'] = 'Could not extract download URL'
                            print(f"[WORKER] FAILED to extract URL for {fi['filename']}")
                    except Exception as e:
                        fi['status'] = 'error'
                        fi['error'] = str(e)
                        print(f"[WORKER] Extract ERROR for {fi['filename']}: {e}")
        except Exception as e:
            print(f"[WORKER] FATAL: Extractor browser failed: {e}")
            for idx in selected_indices:
                fi = session['files'][idx]
                if not fi.get('direct_url') and fi['status'] != 'error':
                    fi['status'] = 'error'
                    fi['error'] = f'Browser launch failed: {e}'

    extractor_thread = threading.Thread(target=extractor_loop, daemon=True)
    extractor_thread.start()

    for idx in selected_indices:
        if session.get('cancelled'):
            break
        fi = session['files'][idx]

        # Extraction runs ahead in the background; only wait if we've caught up to it.
        while not session.get('cancelled') and not fi.get('direct_url') and fi['status'] != 'error':
            time.sleep(0.2)

        if session.get('cancelled'):
            break
        if fi['status'] == 'error' or not fi.get('direct_url'):
            continue

        print(f"[WORKER] Downloading: {fi['filename']}")
        try:
            filepath = os.path.join(download_dir, fi['filename'])
            download_file(fi['direct_url'], filepath, fi, session)
            print(f"[WORKER] Finished: {fi['filename']} -> {fi['status']}")
        except Exception as e:
            fi['status'] = 'error'
            fi['error'] = str(e)
            print(f"[WORKER] Download ERROR for {fi['filename']}: {e}")

    extractor_thread.join(timeout=10)
    session['overall_status'] = 'completed'
    print(f"[WORKER] Worker {session_id} finished")


# ─── Routes ───
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/fetch-links', methods=['POST'])
def api_fetch_links():
    paste_url = request.json.get('paste_url', '').strip()
    if not paste_url:
        return jsonify(success=False, error="No URL provided")
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
        return jsonify(success=False, error=str(e))


@app.route('/api/start-download', methods=['POST'])
def api_start():
    data = request.json
    selected = data.get('selected', [])
    dl_dir = data.get('download_dir', '').strip()
    paste_url = data.get('paste_url', '').strip()
    if not dl_dir: return jsonify(success=False, error="No download directory")
    if not selected: return jsonify(success=False, error="No files selected")
    if paste_url not in fetch_cache: return jsonify(success=False, error="Fetch links first")

    sid = str(uuid.uuid4())[:8]
    files = []
    for f in fetch_cache[paste_url]:
        files.append({
            "url": f["url"], "filename": f["filename"],
            "selected": f["index"] in selected,
            "status": "queued", "progress": 0,
            "downloaded_bytes": 0, "total_bytes": 0,
            "speed": 0, "speed_str": "", "eta": "--:--",
            "error": "", "attempt": 0,
            "segments_done": 0, "segments_total": 0
        })
    download_sessions[sid] = {
        "files": files, "download_dir": dl_dir,
        "overall_status": "running", "cancelled": False
    }
    threading.Thread(target=download_worker, args=(sid,), daemon=True).start()
    return jsonify(success=True, session_id=sid)


@app.route('/api/progress/<sid>')
def api_progress(sid):
    if sid not in download_sessions:
        return jsonify(success=False, error="Session not found")
    s = download_sessions[sid]
    return jsonify(
        success=True,
        overall_status=s['overall_status'],
        files=[{
            'filename': f['filename'], 'selected': f['selected'],
            'status': f['status'], 'progress': f['progress'],
            'downloaded_bytes': f['downloaded_bytes'],
            'total_bytes': f['total_bytes'],
            'speed_str': f.get('speed_str', ''),
            'eta': f.get('eta', '--:--'),
            'error': f.get('error', ''),
            'attempt': f.get('attempt', 0),
            'segments_done': f.get('segments_done', 0),
            'segments_total': f.get('segments_total', 0),
            'downloaded_display': fmt_size(f['downloaded_bytes']),
            'total_display': fmt_size(f['total_bytes']) if f['total_bytes'] else '?'
        } for f in s['files']]
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


if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')
