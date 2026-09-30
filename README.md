# FitGirl Auto Downloader

A high-speed, multi-threaded automated download manager for FitGirl Repacks (hosted on `fuckingfast.co`). Features ad-bypass automation, parallel segment downloading, automatic resumption, network connection monitoring, and a real-time web dashboard.

---

## 🚀 Features

- ⚡ **Automated Link & Ad Bypass**: Uses Playwright / Patchright to auto-extract direct links and bypass multi-step click ad flows and Cloudflare bot protection.
- 🏎️ **Parallel Multi-Segment Downloader**: Accelerates download speeds by chunking files into parallel byte-range requests via HTTP/2 (`httpx`).
- 🔄 **Auto-Resume & Failure Recovery**: Seamlessly resumes interrupted downloads with range requests and handles automatic retries with backoff.
- 🌐 **Network Monitoring**: Continuous connection check using `psutil` and DNS pings; pauses downloads gracefully when offline and auto-resumes when reconnected.
- 📊 **Real-Time Web Dashboard**: Clean Flask web interface with live transfer speed charts, segment progress bars, ETAs, and download queue controls.

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
   Navigate to `http://localhost:5000` in your browser.

3. **Download**:
   - Paste a FitGirl / `fuckingfast.co` paste link.
   - Click **Fetch Links** to inspect available files.
   - Select your target directory and files, then click **Start Download**.

---

## ⚙️ Tech Stack

- **Backend**: Python 3, Flask, HTTPX (HTTP/2), Psutil, ThreadPoolExecutor
- **Automation**: Patchright / Playwright Chromium
- **Frontend**: HTML5, Vanilla CSS3, Javascript, Chart.js
