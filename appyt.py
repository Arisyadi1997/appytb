#!/usr/bin/env python3
"""
Single-file Streamlit app untuk upload & streaming video besar (tanpa input tanggal/jam).
- Membuat .streamlit/config.toml otomatis (maxUploadSize = 4096 MB).
- Menyimpan upload langsung ke disk (folder ./uploads).
- Menjalankan ffmpeg di background, menampilkan log.
- Stop streaming melakukan terminate pada proses ffmpeg yang dibuka oleh app ini.
"""

import os
import sys
import subprocess
import threading
import time
import tempfile
from pathlib import Path

# -----------------------------
# 1) Pastikan file config Streamlit dibuat SEBELUM import streamlit
# -----------------------------
CONFIG_DIR = Path(".streamlit")
CONFIG_PATH = CONFIG_DIR / "config.toml"
if not CONFIG_PATH.exists():
    try:
        CONFIG_DIR.mkdir(exist_ok=True)
        CONFIG_PATH.write_text(
            "[server]\n"
            "maxUploadSize = 4096\n"         # size in MB (4 GB)
            "enableXsrfProtection = false\n"
        )
        print(f"Created Streamlit config at: {CONFIG_PATH}")
    except Exception as e:
        # Jika tidak bisa menulis, kita tetap lanjut (tapi upload limit mungkin tetap kecil)
        print("Gagal membuat .streamlit/config.toml:", e)

# -----------------------------
# 2) Import / install streamlit jika perlu
# -----------------------------
try:
    import streamlit as st
    import streamlit.components.v1 as components
except Exception:
    # Install streamlit lalu import (hati-hati: environment harus mengizinkan pip install)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "streamlit"])
    import streamlit as st
    import streamlit.components.v1 as components

# st.set_page_config harus panggilan Streamlit pertama setelah import.
st.set_page_config(page_title="Streaming YT by didinchy", page_icon="📺", layout="wide")

# -----------------------------
# 3) Utility & Helper
# -----------------------------
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

def ffmpeg_available():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False

def save_uploaded_file(uploaded_file, dest_path, progress_callback=None):
    """
    Simpan uploaded_file (streamlit UploadedFile) ke dest_path dalam chunk agar tidak
    menghabiskan RAM sekaligus.
    progress_callback(bytes_written, total) -> optional
    """
    # uploaded_file.seek(0) mungkin tidak selalu tersedia, tapi mencoba.
    try:
        uploaded_file.seek(0)
    except Exception:
        pass

    # total size, jika ada
    total = getattr(uploaded_file, "size", None) or getattr(uploaded_file, "getbuffer", lambda: None)()
    if isinstance(total, memoryview):
        total = len(total)
    # Tulis per chunk
    chunk_size = 1024 * 1024  # 1 MB
    written = 0
    with open(dest_path, "wb") as f:
        while True:
            chunk = uploaded_file.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
            if progress_callback:
                try:
                    progress_callback(written, total)
                except Exception:
                    pass
    return dest_path

# -----------------------------
# 4) FFmpeg runner (non-blocking)
# -----------------------------
def start_ffmpeg(video_path: str, stream_key: str, is_shorts: bool, log_writer):
    """
    Start ffmpeg process and stream stdout lines to log_writer (callable).
    Returns subprocess.Popen object.
    """
    output_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
    scale_arg = ["-vf", "scale=720:1280"] if is_shorts else []
    cmd = [
        "ffmpeg",
        "-re",
        "-stream_loop", "-1",
        "-i", str(video_path),
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", "2500k",
        "-maxrate", "2500k", "-bufsize", "5000k",
        "-g", "60", "-keyint_min", "60",
        "-c:a", "aac", "-b:a", "128k",
        *scale_arg,
        "-f", "flv",
        output_url
    ]
    log_writer(f"Menjalankan ffmpeg: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except FileNotFoundError:
        log_writer("Error: ffmpeg tidak ditemukan. Pastikan ffmpeg terinstall dan ada di PATH.")
        return None

    # Thread untuk membaca stdout agar tidak blocking
    def reader_thread(p, writer):
        try:
            if p.stdout:
                for line in iter(p.stdout.readline, ''):
                    if not line:
                        break
                    writer(line.strip())
        except Exception as e:
            writer(f"Error membaca ffmpeg output: {e}")
        finally:
            writer(">> Proses ffmpeg berakhir.")
    t = threading.Thread(target=reader_thread, args=(proc, log_writer), daemon=True)
    t.start()
    return proc

# -----------------------------
# 5) Streamlit UI
# -----------------------------
def main():
    st.title("🎬 Streaming YT — Video Besar (No Schedule)")
    st.markdown("Upload video besar (sampai 4GB diatur via `.streamlit/config.toml`). "
                "Pastikan `ffmpeg` terinstall di server/PC Anda.")

    # Cek ffmpeg
    if not ffmpeg_available():
        st.warning("ffmpeg tidak terdeteksi di PATH. Silakan install ffmpeg untuk melakukan streaming.")
        st.info("Contoh install (Ubuntu): `sudo apt update && sudo apt install ffmpeg`")

    # Ads opsional (tetap ada, user bisa toggle)
    show_ads = st.checkbox("Tampilkan Iklan (opsional)", value=False)
    if show_ads:
        components.html(
            """
            <div style="background:#f0f2f6;padding:12px;border-radius:8px;text-align:center">
                <p style="color:#666">Iklan (contoh)</p>
            </div>
            """,
            height=120
        )

    # Daftar file di folder uploads + direktori kerja
    local_videos = [str(p) for p in Path(".").glob("*.mp4")] + [str(p) for p in Path(".").glob("*.flv")]
    uploads_videos = [str(p) for p in UPLOAD_DIR.glob("*") if p.suffix.lower() in (".mp4", ".flv")]
    all_videos = uploads_videos + local_videos
    st.write("📁 Video yang tersedia:")
    selected_video = st.selectbox("Pilih video dari server (optional)", options=["-- pilih --"] + all_videos, index=0)

    uploaded_file = st.file_uploader("Atau upload video baru (mp4/flv - codec H264/AAC)", type=['mp4', 'flv'])

    # Jika ada upload, simpan ke disk
    saved_path = None
    progress_placeholder = st.empty()
    if uploaded_file is not None:
        # Tentukan path tujuan
        dest_path = UPLOAD_DIR / uploaded_file.name
        # Jika file sudah ada, tambahkan timestamp
        if dest_path.exists():
            stamp = int(time.time())
            dest_path = UPLOAD_DIR / f"{dest_path.stem}_{stamp}{dest_path.suffix}"

        progress_bar = st.progress(0)
        status_text = st.empty()
        def progress_cb(written, total):
            if total and total > 0:
                frac = min(1.0, written / total)
                progress_bar.progress(frac)
                status_text.text(f"Mengupload: {written}/{total} bytes")
            else:
                # Unknown total, show spinner style
                status_text.text(f"Mengupload: {written} bytes")
        try:
            save_uploaded_file(uploaded_file, dest_path, progress_callback=progress_cb)
            progress_bar.progress(1.0)
            status_text.success(f"Upload selesai: {dest_path}")
            saved_path = str(dest_path)
        except Exception as e:
            status_text.error(f"Gagal menyimpan file: {e}")
            saved_path = None
        progress_placeholder = status_text

    # Pilih final video path (uploaded atau yang ada)
    if selected_video and selected_video != "-- pilih --":
        video_path = selected_video
    elif saved_path:
        video_path = saved_path
    else:
        video_path = None

    st.write("")  # spacing
    stream_key = st.text_input("Stream Key (YouTube)", type="password")
    is_shorts = st.checkbox("Mode Shorts (720x1280)")

    # Log area
    log_box = st.empty()
    logs = st.session_state.get("logs", [])
    def append_log(msg):
        logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        # keep last 200 lines to avoid huge memory
        logs_trimmed = logs[-200:]
        st.session_state["logs"] = logs_trimmed
        log_box.text("\n".join(logs_trimmed))

    # State for ffmpeg process
    proc = st.session_state.get("ffmpeg_proc", None)
    running = proc is not None and getattr(proc, "poll", lambda: 1)() is None

    # Buttons: Start / Stop
    col1, col2 = st.columns([1,1])
    with col1:
        start_btn = st.button("🎬 Mulai Streaming")
    with col2:
        stop_btn = st.button("🛑 Hentikan Streaming")

    if start_btn:
        if not video_path:
            st.error("Pilih video atau upload dulu sebelum memulai streaming.")
        elif not stream_key:
            st.error("Masukkan Stream Key YouTube.")
        elif not ffmpeg_available():
            st.error("ffmpeg tidak tersedia. Install ffmpeg dulu.")
        else:
            # Jika proses sebelumnya jalan, jangan start ganda
            proc = st.session_state.get("ffmpeg_proc", None)
            if proc and getattr(proc, "poll", lambda: 0)() is None:
                st.warning("Proses ffmpeg sedang berjalan. Hentikan dulu sebelum memulai baru.")
            else:
                append_log(f"Memulai streaming: {video_path}")
                proc = start_ffmpeg(video_path, stream_key, is_shorts, append_log)
                if proc:
                    st.session_state["ffmpeg_proc"] = proc
                    st.success("Streaming dimulai.")
                else:
                    st.error("Gagal menjalankan ffmpeg. Periksa log.")

    if stop_btn:
        proc = st.session_state.get("ffmpeg_proc", None)
        if proc and getattr(proc, "poll", lambda: 0)() is None:
            append_log("Menghentikan proses ffmpeg...")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                append_log("Proses ffmpeg dihentikan.")
            except Exception as e:
                append_log(f"Gagal menghentikan ffmpeg: {e}")
            # bersihkan state
            st.session_state["ffmpeg_proc"] = None
        else:
            st.info("Tidak ada proses ffmpeg aktif.")

    # Tampilkan log awal (jika ada)
    if "logs" in st.session_state:
        log_box.text("\n".join(st.session_state["logs"]))
    else:
        log_box.text("Log kosong. Klik 'Mulai Streaming' untuk melihat output ffmpeg.")

    # Info file path
    if video_path:
        st.info(f"Video yang akan di-stream: {video_path}")

    # Footer / tips
    st.markdown("---")
    st.markdown(
        "- Tip: app ini akan menulis file upload ke folder `./uploads`.\n"
        "- Jika dijalankan di Streamlit Cloud, batas upload tetap bisa dibatasi oleh platform.\n"
        "- Pastikan koneksi/CPU memadai untuk streaming video durasi panjang.\n"
    )

if __name__ == "__main__":
    main()
