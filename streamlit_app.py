import glob
import io
import os
import re
import tempfile
import bcrypt
import streamlit as st
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPE1
import yt_dlp
from supabase import Client, create_client

st.set_page_config(
    page_title="YouTube to MP3 & Tag Editor",
    page_icon="🎵",
    layout="centered",
)


# --- 1. SUPABASE CONNECTION ---
@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)


supabase = init_supabase()


# --- 2. SESSION STATE INITIALIZATION ---
DEFAULT_STATE = {
    "authenticated": False,
    "username": "",
    "yt_mp3_bytes": None,
    "yt_filename": "",
    "yt_meta": {"title": "", "artist": "", "album": "", "year": ""},
    "yt_cover_bytes": None,
    "yt_cover_mime": "image/jpeg",
}
for key, val in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = val


# --- 3. AUTHENTICATION ---
def verify_credentials(username: str, password: str) -> bool:
    try:
        res = (
            supabase.table("users")
            .select("password")
            .eq("username", username.strip())
            .execute()
        )
        if not res.data:
            return False

        stored_hash = res.data[0]["password"]
        return bcrypt.checkpw(
            password.encode("utf-8"), stored_hash.encode("utf-8")
        )
    except Exception as e:
        st.error(f"Database connection error: {e}")
        return False


def render_login_screen():
    st.title("🔒 Sign In")
    st.write("Log in to access the YouTube Audio Converter & Tag Editor.")

    with st.form("login_form"):
        user_input = st.text_input("Username")
        pass_input = st.text_input("Password", type="password")
        submitted = st.form_submit_button(
            "Log In", type="primary", use_container_width=True
        )

        if submitted:
            if not user_input or not pass_input:
                st.warning("Please enter both username and password.")
            elif verify_credentials(user_input, pass_input):
                st.session_state.authenticated = True
                st.session_state.username = user_input.strip()
                st.rerun()
            else:
                st.error("Invalid username or password.")


# --- 4. ID3v2.3 METADATA HELPERS (MUTAGEN) ---
def read_mp3_metadata(mp3_bytes: bytes) -> tuple[dict, bytes | None, str]:
    """Reads ID3 tags and embedded APIC cover art from raw MP3 bytes."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(mp3_bytes)
        tmp_path = tmp.name

    meta = {"title": "", "artist": "", "album": "", "year": ""}
    cover_bytes = None
    cover_mime = "image/jpeg"

    try:
        tags = ID3(tmp_path)
        meta["title"] = str(tags.get("TIT2", ""))
        meta["artist"] = str(tags.get("TPE1", ""))
        meta["album"] = str(tags.get("TALB", ""))
        meta["year"] = str(tags.get("TDRC", ""))

        apic_frames = tags.getall("APIC")
        if apic_frames:
            cover_bytes = apic_frames[0].data
            cover_mime = apic_frames[0].mime or "image/jpeg"
    except ID3NoHeaderError:
        pass
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return meta, cover_bytes, cover_mime


def write_mp3_metadata_v23(
    mp3_bytes: bytes,
    title: str,
    artist: str,
    album: str,
    year: str,
    cover_bytes: bytes | None = None,
    cover_mime: str = "image/jpeg",
) -> bytes:
    """Writes ID3v2.3 tags and replaces APIC cover art, returning updated MP3 bytes."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(mp3_bytes)
        tmp_path = tmp.name

    try:
        try:
            tags = ID3(tmp_path)
        except ID3NoHeaderError:
            tags = ID3()

        # Replace standard text frames
        tags.delall("TIT2")
        tags.delall("TPE1")
        tags.delall("TALB")
        tags.delall("TDRC")

        if title.strip():
            tags.add(TIT2(encoding=3, text=title.strip()))
        if artist.strip():
            tags.add(TPE1(encoding=3, text=artist.strip()))
        if album.strip():
            tags.add(TALB(encoding=3, text=album.strip()))
        if year.strip():
            tags.add(TDRC(encoding=3, text=year.strip()))

        # Replace cover art cleanly without duplicating APIC frames
        if cover_bytes:
            tags.delall("APIC")
            tags.add(
                APIC(
                    encoding=3,
                    mime=cover_mime,
                    type=3,  # Front Cover
                    desc="Cover",
                    data=cover_bytes,
                )
            )

        # Save explicitly as ID3v2.3 for universal OS/player compatibility
        tags.save(tmp_path, v2_version=3)

        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return cleaned or "audio_track"


# --- 5. YOUTUBE EXTRACTION LOGIC ---
def clean_youtube_url(url: str) -> str:
    """Extracts the 11-character video ID and strips radio/playlist query parameters."""
    url = url.strip()
    match = re.search(r"(?:v=|\/|youtu\.be\/)([0-9A-Za-z_-]{11})", url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    return url


def extract_youtube_mp3(
    video_url: str,
    quality: str,
    progress_bar,
    status_box,
    log_area,
) -> tuple[bytes, str]:
    temp_dir = tempfile.mkdtemp()
    out_template = os.path.join(temp_dir, "%(title)s.%(ext)s")
    clean_url = clean_youtube_url(video_url)
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
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                fraction = min(downloaded / total, 1.0)
                progress_bar.progress(fraction)
                status_box.info(
                    f"Downloading stream: {fraction * 100:.1f}% ({d.get('_speed_str', '')})"
                )
            else:
                status_box.info(
                    f"Downloading stream: {d.get('_downloaded_bytes_str', '...')} received"
                )
        elif d["status"] == "finished":
            progress_bar.progress(1.0)
            status_box.info(
                "Download complete. Transcoding to MP3 & embedding thumbnail..."
            )

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "writethumbnail": True,
        "progress_hooks": [progress_hook],
        "logger": StreamlitLogger(),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
