import os
import glob
import re
import tempfile
import streamlit as st
import yt_dlp

st.set_page_config(page_title="YouTube to MP3", page_icon="🎵", layout="centered")

st.title("🎵 YouTube to MP3 Converter")
st.write("Extract audio tracks with embedded ID3 metadata and cover art.")

raw_url = st.text_input("YouTube URL:", placeholder="https://www.youtube.com/watch?v=...")
bitrate = st.selectbox(
    "Target Audio Bitrate:",
    options=["128", "192", "256", "320"],
    index=1
)

if "converted_file" not in st.session_state:
    st.session_state.converted_file = None
if "download_name" not in st.session_state:
    st.session_state.download_name = ""

status_box = st.empty()
progress_bar = st.empty()
log_expander = st.expander("Show detailed logs", expanded=False)
log_area = log_expander.empty()
log_messages = []

class StreamlitLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        self._log(msg)

    def warning(self, msg):
        self._log(f"⚠️ {msg}")

    def error(self, msg):
        self._log(f"❌ {msg}")

    def _log(self, msg):
        log_messages.append(msg)
        log_area.text("\n".join(log_messages[-12:]))

def progress_hook(d):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        downloaded = d.get('downloaded_bytes', 0)
        if total > 0:
            fraction = min(downloaded / total, 1.0)
            progress_bar.progress(fraction)
            status_box.info(f"Downloading stream: {fraction * 100:.1f}% ({d.get('_speed_str', '')})")
        else:
            status_box.info(f"Downloading: {d.get('_downloaded_bytes_str', '...')} received")
    elif d['status'] == 'finished':
        progress_bar.progress(1.0)
        status_box.info("Download complete. Transcoding to MP3 & embedding cover art...")

def clean_youtube_url(url: str) -> str:
    """Strips playlist, radio mix, and tracking parameters that cause 404s."""
    url = url.strip()
    match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    return url

def process_youtube_audio(video_url: str, quality: str):
    temp_dir = tempfile.mkdtemp()
    out_template = os.path.join(temp_dir, "%(title)s.%(ext)s")
    
    clean_url = clean_youtube_url(video_url)
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_template,
        'writethumbnail': True,
        'progress_hooks': [progress_hook],
        'logger': StreamlitLogger(),
        'postprocessors': [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': quality,
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            },
            {
                'key': 'EmbedThumbnail',
                'already_have_thumbnail': False,
            },
        ],
        # TV and MWEB clients bypass datacenter IP restrictions
        'extractor_args': {
            'youtube': {
                'player_client': ['tv', 'mweb', 'web'],
            }
        },
        'socket_timeout': 30,
        'retries': 3,
        'no_warnings': False,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(clean_url, download=True)
        title = info.get('title', 'audio')
        
    mp3_files = glob.glob(os.path.join(temp_dir, "*.mp3"))
    if not mp3_files:
        raise RuntimeError("No MP3 file was created. Check the log details above.")
    
    return mp3_files[0], f"{title}.mp3"

if st.button("Convert to MP3", type="primary"):
    if not raw_url.strip():
        st.warning("Please provide a valid YouTube URL.")
    else:
        log_messages.clear()
        progress_bar.progress(0.0)
        status_box.info("Connecting to YouTube...")
        
        try:
            file_path, display_name = process_youtube_audio(raw_url, bitrate)
            
            with open(file_path, "rb") as f:
                st.session_state.converted_file = f.read()
            st.session_state.download_name = display_name
            
            status_box.success("Finished! Ready to download below.")
        except Exception as e:
            status_box.error(f"Processing failed: {e}")

if st.session_state.converted_file:
    st.download_button(
        label=f"⬇️ Download {st.session_state.download_name}",
        data=st.session_state.converted_file,
        file_name=st.session_state.download_name,
        mime="audio/mpeg"
    )
