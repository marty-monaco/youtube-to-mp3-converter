import os
import io
import glob
import re
import tempfile
import streamlit as st
import yt_dlp
from mutagen.id3 import (
    ID3,
    ID3NoHeaderError,
    TIT2,
    TPE1,
    TALB,
    TPE2,
    TCON,
    TDRC,
    TRCK,
    COMM,
    APIC,
)
from mutagen.mp3 import MP3

st.set_page_config(page_title="YouTube to MP3 & Tag Editor", page_icon="🎵", layout="centered")

# ---------------------------------------------------------------------------
# ID3 Metadata Helpers & Reusable Editor UI
# ---------------------------------------------------------------------------
def get_tag_text(tags: ID3, frame_id: str) -> str:
    frame = tags.get(frame_id)
    if frame and hasattr(frame, "text") and frame.text:
        return str(frame.text[0])
    return ""


def get_comment_text(tags: ID3) -> str:
    for key in tags.keys():
        if key.startswith("COMM"):
            frame = tags.get(key)
            if frame and frame.text:
                return str(frame.text[0])
    return ""


def get_cover_art(tags: ID3) -> bytes | None:
    for key in tags.keys():
        if key.startswith("APIC"):
            return tags[key].data
    return None


def update_mp3_metadata(
    file_bytes: bytes,
    title: str,
    artist: str,
    album: str,
    album_artist: str,
    genre: str,
    year: str,
    track: str,
    comment: str,
    new_cover_bytes: bytes | None = None,
    new_cover_mime: str = "image/jpeg",
) -> bytes:
    buf = io.BytesIO(file_bytes)

    try:
        tags = ID3(buf)
    except ID3NoHeaderError:
        tags = ID3()

    def set_or_del(frame_cls, frame_id: str, val: str):
        val = val.strip()
        if val:
            tags[frame_id] = frame_cls(encoding=3, text=val)
        elif frame_id in tags:
            del tags[frame_id]

    set_or_del(TIT2, "TIT2", title)
    set_or_del(TPE1, "TPE1", artist)
    set_or_del(TALB, "TALB", album)
    set_or_del(TPE2, "TPE2", album_artist)
    set_or_del(TCON, "TCON", genre)
    set_or_del(TDRC, "TDRC", year)
    set_or_del(TRCK, "TRCK", track)

    for k in [k for k in tags.keys() if k.startswith("COMM")]:
        del tags[k]
    if comment.strip():
        tags.add(COMM(encoding=3, lang="eng", desc="", text=comment.strip()))

    if new_cover_bytes:
        for k in [k for k in tags.keys() if k.startswith("APIC")]:
            del tags[k]
        tags.add(
            APIC(
                encoding=3,
                mime=new_cover_mime,
                type=3,
                desc="Cover",
                data=new_cover_bytes,
            )
        )

    buf.seek(0)
    tags.save(buf, v2_version=3)
    buf.seek(0)
    return buf.read()


def render_tag_editor(raw_bytes: bytes, default_filename: str, key_prefix: str):
    """Renders an interactive ID3 metadata editor + download button for any MP3 byte buffer."""
    buf = io.BytesIO(raw_bytes)

    try:
        audio_info = MP3(buf)
        duration_sec = int(audio_info.info.length)
        mins, secs = divmod(duration_sec, 60)
        bitrate_kbps = int(audio_info.info.bitrate / 1000)
        st.caption(
            f"⏱️ {mins}:{secs:02d}  •  🎚️ {bitrate_kbps} kbps  •  🔊 {audio_info.info.sample_rate:,} Hz"
        )
    except Exception as e:
        st.warning(f"Could not read MP3 stream info: {e}")

    buf.seek(0)
    try:
        tags = ID3(buf)
    except ID3NoHeaderError:
        tags = ID3()
        st.info("No existing ID3 header found—saving will create a fresh ID3v2.3 tag header.")

    existing_cover = get_cover_art(tags)
    if existing_cover:
        st.image(existing_cover, width=140, caption="Embedded Cover Art")

    with st.form(key=f"{key_prefix}_id3_form"):
        st.subheader("🏷️ Edit ID3 Metadata")
        title = st.text_input("Title", value=get_tag_text(tags, "TIT2"))
        artist = st.text_input("Artist", value=get_tag_text(tags, "TPE1"))
        album = st.text_input("Album", value=get_tag_text(tags, "TALB"))

        col1, col2 = st.columns(2)
        with col1:
            album_artist = st.text_input("Album Artist", value=get_tag_text(tags, "TPE2"))
            year = st.text_input("Year", value=get_tag_text(tags, "TDRC"))
        with col2:
            genre = st.text_input("Genre", value=get_tag_text(tags, "TCON"))
            track = st.text_input(
                "Track #", value=get_tag_text(tags, "TRCK"), placeholder="e.g. 1 or 1/12"
            )

        comment = st.text_area("Comment / Notes", value=get_comment_text(tags), height=80)
        new_cover = st.file_uploader(
            "Replace Cover Art (optional):",
            type=["jpg", "jpeg", "png"],
            key=f"{key_prefix}_cover",
        )

        submitted = st.form_submit_button("💾 Apply Metadata Changes", use_container_width=True)

    edited_key = f"{key_prefix}_edited_bytes"
    if submitted:
        cover_bytes = new_cover.read() if new_cover else None
        cover_mime = new_cover.type if new_cover else "image/jpeg"

        st.session_state[edited_key] = update_mp3_metadata(
            file_bytes=raw_bytes,
            title=title,
            artist=artist,
            album=album,
            album_artist=album_artist,
            genre=genre,
            year=year,
            track=track,
            comment=comment,
            new_cover_bytes=cover_bytes,
            new_cover_mime=cover_mime,
        )
        st.success("Metadata updated!")

    # Use edited bytes if user saved tag changes; otherwise download current MP3
    final_bytes = st.session_state.get(edited_key, raw_bytes)
    safe_out_name = f"{title.strip()}.mp3" if submitted and title.strip() else default_filename

    st.download_button(
        label=f"⬇️ Download {safe_out_name}",
        data=final_bytes,
        file_name=safe_out_name,
        mime="audio/mpeg",
        key=f"{key_prefix}_dl_btn",
        use_container_width=True,
    )


# ---------------------------------------------------------------------------
# YouTube Extraction Helpers
# ---------------------------------------------------------------------------
def clean_youtube_url(url: str) -> str:
    url = url.strip()
    match = re.search(r"(?:v=|\/|youtu\.be\/)([0-9A-Za-z_-]{11})", url)
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
        'socket_timeout': 30,
        'retries': 5,
        'no_warnings': False,
    }

    # Load cookies from Streamlit Secrets if configured
    if "YOUTUBE_COOKIES" in st.secrets and st.secrets["YOUTUBE_COOKIES"].strip():
        cookie_path = os.path.join(temp_dir, "cookies.txt")
        with open(cookie_path, "w", encoding="utf-8") as cf:
            cf.write(st.secrets["YOUTUBE_COOKIES"].strip())
        ydl_opts['cookiefile'] = cookie_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(clean_url, download=True)
        title = info.get('title', 'audio')

    mp3_files = glob.glob(os.path.join(temp_dir, "*.mp3"))
    if not mp3_files:
        raise RuntimeError("No MP3 file was created. Check the log details above.")

    return mp3_files[0], f"{title}.mp3"


# ---------------------------------------------------------------------------
# Main UI Layout
# ---------------------------------------------------------------------------
st.title("🎵 YouTube to MP3 & Tag Editor")
st.write("Convert YouTube audio to MP3 or edit ID3 metadata and cover art on any MP3.")

if "converted_file" not in st.session_state:
    st.session_state.converted_file = None
if "download_name" not in st.session_state:
    st.session_state.download_name = ""

tab_yt, tab_upload = st.tabs(["🎬 YouTube to MP3", "🏷️ Edit Existing MP3"])

with tab_yt:
    raw_url = st.text_input("YouTube URL:", placeholder="https://www.youtube.com/watch?v=...")
    bitrate = st.selectbox(
        "Target Audio Bitrate:",
        options=["128", "192", "256", "320"],
        index=1,
    )

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
            status_box.info("Download complete. Transcoding to MP3 & embedding cover art...")

    if st.button("Convert to MP3", type="primary"):
        if not raw_url.strip():
            st.warning("Please provide a valid YouTube URL.")
        else:
            log_messages.clear()
            st.session_state.pop("yt_edited_bytes", None)
            progress_bar.progress(0.0)
            status_box.info("Connecting to YouTube...")

            try:
                file_path, display_name = process_youtube_audio(
                    raw_url, bitrate, progress_hook, StreamlitLogger()
                )
                with open(file_path, "rb") as f:
                    st.session_state.converted_file = f.read()
                st.session_state.download_name = display_name

                status_box.success("Finished! Edit tags below or download immediately.")
            except Exception as e:
                status_box.error(f"Processing failed: {e}")

    if st.session_state.converted_file:
        st.divider()
        render_tag_editor(
            raw_bytes=st.session_state.converted_file,
            default_filename=st.session_state.download_name,
            key_prefix="yt",
        )

with tab_upload:
    uploaded_mp3 = st.file_uploader("Upload an MP3 file:", type=["mp3"])
    if uploaded_mp3:
        upload_bytes = uploaded_mp3.read()
        # Reset cached edits if a different file is uploaded
        if st.session_state.get("last_uploaded_name") != uploaded_mp3.name:
            st.session_state["last_uploaded_name"] = uploaded_mp3.name
            st.session_state.pop("up_edited_bytes", None)

        render_tag_editor(
            raw_bytes=upload_bytes,
            default_filename=uploaded_mp3.name,
            key_prefix="up",
        )
