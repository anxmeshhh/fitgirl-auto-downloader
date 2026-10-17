# FitGirl Auto Downloader

A high-speed, multi-threaded automated download manager for FitGirl Repacks (hosted on `fuckingfast.co`). Features ad-bypass automation, parallel segment downloading, automatic resumption, network connection monitoring, and a real-time web dashboard.

---

## 🚀 Features

- ⚡ **Automated Link & Ad Bypass**: Uses Playwright / Patchright to auto-extract direct links and bypass multi-step click ad flows and Cloudflare bot protection.
- 🏎️ **Parallel Multi-Segment Downloader**: Accelerates download speeds by chunking files into parallel byte-range requests via HTTP/2 (`httpx`).
- 🔄 **Auto-Resume & Failure Recovery**: Seamlessly resumes interrupted downloads with range requests and handles automatic retries with backoff.
- 🌐 **Network Monitoring**: Continuous connection check using DNS pings; pauses downloads when offline and resumes when reconnected, without using up retry attempts.
- 🔗 **Expired Link Recovery**: Direct links are extracted just ahead of the downloader and re-extracted automatically if they expire mid-download.
- ⏭️ **Skips Finished Files**: Re-running a download skips parts that are already complete; **Retry failed** re-runs only what didn't finish.
- 📊 **Real-Time Web Dashboard**: Live speed graph of this app's own transfers, per-file progress, ETAs, and a session that survives page refreshes.

---

## 🛠️ Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/anxmeshhh/fitgirl-auto-downloader.git
   cd fitgirl-auto-downloader
   ```

2. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Install Browser Binaries** (Required for link extraction):
   ```bash
   patchright install chromium
   ```

---

## 💻 Usage

1. **Start the Flask server**:
   ```bash
   python app.py
   ```

2. **Open the Dashboard**:
   Navigate to `http://localhost:5000` in your browser. The server only listens on `127.0.0.1`; set `FITGRAB_DEBUG=1` to enable Flask debug mode.

3. **Download**:
   - Paste a FitGirl / `fuckingfast.co` paste link.
   - Click **Fetch Links** to inspect available files.
   - Enter a full folder path under **Save to**, select files, then click **Start download**.

---

## 🧪 Tests

```bash
pip install pytest
python -m pytest tests
```

---

## ⚙️ Tech Stack

- **Backend**: Python 3, Flask, HTTPX (HTTP/2), Psutil, ThreadPoolExecutor
- **Automation**: Patchright / Playwright Chromium
- **Frontend**: HTML5, vanilla CSS and JavaScript (canvas speed graph)
