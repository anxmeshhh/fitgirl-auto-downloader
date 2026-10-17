import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as fg  # noqa: E402

PAYLOAD = os.urandom(12 * 1024 * 1024 + 123)  # just over SEGMENT_MIN_FILE, uneven split


# ─── Pure helpers ───
@pytest.mark.parametrize("raw, expected", [
    ("game.part01.rar", "game.part01.rar"),
    ("..\\..\\Startup\\evil.bat", "evil.bat"),
    ("../../etc/passwd", "passwd"),
    ("C:\\Windows\\x.dll", "x.dll"),
    ("..", "unknown_file"),
    ("", "unknown_file"),
    ("a%2F..%2Fb.rar", "b.rar"),
    ('bad<>:"|?*name.rar', "bad_______name.rar"),
    ("CON.txt", "_CON.txt"),
    ("trailing. ", "trailing"),
])
def test_safe_filename(raw, expected):
    assert fg.safe_filename(raw) == expected


def test_parse_filename():
    assert fg.parse_filename("https://fuckingfast.co/abc#Game.part1.rar") == "Game.part1.rar"
    assert fg.parse_filename("https://fuckingfast.co/abc#..\\..\\x.rar") == "x.rar"
    assert fg.parse_filename("https://host/path/file.bin?x=1") == "file.bin"


@pytest.mark.parametrize("total, n", [(100, 8), (7, 8), (12 * 1024 * 1024 + 123, 8), (8, 8)])
def test_split_ranges_cover_everything(total, n):
    ranges = fg.split_ranges(total, n)
    assert ranges[0][0] == 0 and ranges[-1][1] == total - 1
    for (_, prev_end), (start, _) in zip(ranges, ranges[1:]):
        assert start == prev_end + 1


def test_formatters():
    assert fg.fmt_speed(512) == "512 B/s"
    assert fg.fmt_speed(2.5 * 1024**2) == "2.5 MB/s"
    assert fg.fmt_size(3 * 1024**3) == "3.00 GB"
    assert fg.fmt_eta(0) == "--:--"
    assert fg.fmt_eta(75) == "1:15"
    assert fg.fmt_eta(3725) == "1:02:05"


# ─── Download engine against a local server ───
class Handler(BaseHTTPRequestHandler):
    honor_ranges = True
    status_override = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        if Handler.status_override:
            self.send_response(Handler.status_override)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng and Handler.honor_ranges:
            start, _, end = rng.split("=")[1].partition("-")
            start = int(start)
            end = int(end) if end else len(PAYLOAD) - 1
            if start >= len(PAYLOAD):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = PAYLOAD[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
        else:
            body = PAYLOAD
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    Handler.honor_ranges, Handler.status_override = True, None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/file.bin"
    srv.shutdown()


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(fg, "RETRY_DELAYS", [0])
    fg.net_monitor.connected = True


def new_file_info(url):
    return {"filename": "file.bin", "direct_url": url, "status": "queued", "progress": 0,
            "downloaded_bytes": 0, "total_bytes": 0, "error": "", "note": "", "attempt": 0}


def run(file_info, path, refresh_url=lambda: None):
    fg.download_file(file_info, str(path), {"cancelled": False}, refresh_url)
    return file_info


def read(path):
    with open(path, "rb") as f:
        return f.read()


def test_multisegment_download(server, tmp_path):
    out = tmp_path / "file.bin"
    fi = run(new_file_info(server), out)
    assert fi["status"] == "completed", fi["error"]
    assert fi["segments_total"] == fg.NUM_SEGMENTS
    assert read(out) == PAYLOAD
    assert not any(os.path.exists(p) for p in fg._segment_paths(str(out)))


def test_multisegment_resumes_partial_segments(server, tmp_path):
    out = tmp_path / "file.bin"
    ranges = fg.split_ranges(len(PAYLOAD), fg.NUM_SEGMENTS)
    for path, (start, end) in zip(fg._segment_paths(str(out)), ranges):
        with open(path, "wb") as f:
            f.write(PAYLOAD[start:start + (end - start + 1) // 3])
    fi = run(new_file_info(server), out)
    assert fi["status"] == "completed", fi["error"]
    assert read(out) == PAYLOAD


def test_skips_file_already_downloaded(server, tmp_path):
    out = tmp_path / "file.bin"
    out.write_bytes(PAYLOAD)
    fi = run(new_file_info(server), out)
    assert fi["status"] == "completed"
    assert fi["note"] == "Already downloaded"


def test_single_stream_when_ranges_unsupported(server, tmp_path):
    Handler.honor_ranges = False
    out = tmp_path / "file.bin"
    out.write_bytes(b"stale partial data")
    fi = run(new_file_info(server), out)
    assert fi["status"] == "completed", fi["error"]
    assert read(out) == PAYLOAD


def test_expired_link_is_refreshed(server, tmp_path):
    Handler.status_override = 403
    calls = []

    def refresh():
        calls.append(1)
        Handler.status_override = None
        return server

    fi = run(new_file_info(server), tmp_path / "file.bin", refresh)
    assert calls == [1]
    assert fi["status"] == "completed", fi["error"]


def test_expired_link_refresh_failure(server, tmp_path):
    Handler.status_override = 410
    fi = run(new_file_info(server), tmp_path / "file.bin", lambda: None)
    assert fi["status"] == "error"
    assert "expired" in fi["error"]


# ─── API validation ───
@pytest.fixture
def client():
    fg.fetch_cache["https://paste.example/?a#b"] = [{"index": 0, "url": "u", "filename": "f"}]
    return fg.app.test_client()


def test_start_rejects_relative_dir(client):
    r = client.post("/api/start-download", json={
        "paste_url": "https://paste.example/?a#b", "selected": [0], "download_dir": "games"})
    assert r.get_json()["success"] is False


def test_endpoints_tolerate_non_json(client):
    r = client.post("/api/fetch-links", data="nope", content_type="text/plain")
    assert r.get_json() == {"success": False, "error": "No URL provided"}


def test_retry_unknown_session(client):
    assert client.post("/api/retry/missing").get_json()["success"] is False
