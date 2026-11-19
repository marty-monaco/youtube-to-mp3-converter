import glob
import os
import re
import tempfile
import bcrypt
import streamlit as st
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPE1
from supabase import Client, create_client
import yt_dlp

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
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(mp3_bytes)
        tmp_path = tmp.name

    try:
        try:
            tags = ID3(tmp_path)
        except ID3NoHeaderError:
            tags = ID3()

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

        if cover_bytes:
            tags.delall("APIC")
            tags.add(
                APIC(
                    encoding=3,
                    mime=cover_mime,
                    type=3,
                    desc="Cover",
                    data=cover_bytes,
                )
            )

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
                "preferredquality": quality,
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
            {
                "key": "EmbedThumbnail",
                "already_have_thumbnail": False,
            },
        ],
        "socket_timeout": 30,
        "retries": 5,
        "no_warnings": False,
    }

    if "YOUTUBE_COOKIES" in st.secrets and st.secrets["YOUTUBE_COOKIES"].strip():
        cookie_path = os.path.join(temp_dir, "cookies.txt")
        with open(cookie_path, "w", encoding="utf-8") as cf:
            cf.write(st.secrets["YOUTUBE_COOKIES"].strip() + "\n")
        ydl_opts["cookiefile"] = cookie_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(clean_url, download=True)
        title = sanitize_filename(info.get("title", "audio"))

    mp3_files = glob.glob(os.path.join(temp_dir, "*.mp3"))
    if not mp3_files:
        raise RuntimeError(
            "No MP3 file was produced. Expand the logs above to inspect yt-dlp output."
        )

    with open(mp3_files[0], "rb") as f:
        raw_bytes = f.read()

    return raw_bytes, f"{title}.mp3"


# --- 6. MAIN TWO-TAB APPLICATION ---
def render_main_app():
    col1, col2 = st.columns([4, 1])
    with col1:
        st.title("🎵 YouTube to MP3 & Tag Editor")
    with col2:
        st.write("")
        if st.button("Log Out", use_container_width=True):
            for key, val in DEFAULT_STATE.items():
                st.session_state[key] = val
            st.rerun()

    cookies_active = bool(
        "YOUTUBE_COOKIES" in st.secrets and st.secrets["YOUTUBE_COOKIES"].strip()
    )
    cookie_badge = "Cookies: Active" if cookies_active else "Cookies: Not Configured"
    st.caption(f"Signed in as **{st.session_state.username}**  |  {cookie_badge}")

    tab_yt, tab_edit = st.tabs(["YouTube to MP3", "Edit Existing MP3"])

    # --- TAB 1: YOUTUBE EXTRACTION + POST-CONVERSION TAG EDITOR ---
    with tab_yt:
        raw_url = st.text_input(
            "YouTube URL:",
            placeholder="https://www.youtube.com/watch?v=...",
            key="yt_url_input",
        )
        bitrate = st.selectbox(
            "Target Audio Bitrate (kbps):",
            options=["128", "192", "256", "320"],
            index=1,
            key="yt_bitrate",
        )

        status_box = st.empty()
        progress_bar = st.empty()
        log_expander = st.expander("Show detailed extraction logs", expanded=False)
        log_area = log_expander.empty()

        if st.button("Convert to MP3", type="primary", key="btn_convert_yt"):
            if not raw_url.strip():
                st.warning("Please enter a valid YouTube URL.")
            else:
                progress_bar.progress(0.0)
                status_box.info("Connecting to YouTube...")
                try:
                    mp3_bytes, filename = extract_youtube_mp3(
                        raw_url, bitrate, progress_bar, status_box, log_area
                    )
                    meta, cover_bytes, cover_mime = read_mp3_metadata(mp3_bytes)

                    st.session_state.yt_mp3_bytes = mp3_bytes
                    st.session_state.yt_filename = filename
                    st.session_state.yt_meta = meta
                    st.session_state.yt_cover_bytes = cover_bytes
                    st.session_state.yt_cover_mime = cover_mime

                    status_box.success(
                        "Conversion complete! Refine ID3v2.3 tags below or download directly."
                    )
                except Exception as e:
                    status_box.error(f"Processing failed: {e}")

        if st.session_state.yt_mp3_bytes:
            st.divider()
            st.subheader("Refine ID3v2.3 Tags & Cover Art")

            c_art, c_fields = st.columns([1, 2])
            with c_art:
                if st.session_state.yt_cover_bytes:
                    st.image(
                        st.session_state.yt_cover_bytes,
                        caption="Current Artwork",
                        use_container_width=True,
                    )
                else:
                    st.info("No cover art embedded.")

                new_yt_cover = st.file_uploader(
                    "Replace Cover Art",
                    type=["jpg", "jpeg", "png"],
                    key="yt_cover_upload",
                )

            with c_fields:
                yt_title = st.text_input(
                    "Title",
                    value=st.session_state.yt_meta.get("title", ""),
                    key="yt_edit_title",
                )
                yt_artist = st.text_input(
                    "Artist",
                    value=st.session_state.yt_meta.get("artist", ""),
                    key="yt_edit_artist",
                )
                yt_album = st.text_input(
                    "Album",
                    value=st.session_state.yt_meta.get("album", ""),
                    key="yt_edit_album",
                )
                yt_year = st.text_input(
                    "Year",
                    value=st.session_state.yt_meta.get("year", ""),
                    key="yt_edit_year",
                )

            cover_to_write = st.session_state.yt_cover_bytes
            mime_to_write = st.session_state.yt_cover_mime
            if new_yt_cover is not None:
                cover_to_write = new_yt_cover.read()
                mime_to_write = new_yt_cover.type or "image/jpeg"

            final_yt_bytes = write_mp3_metadata_v23(
                st.session_state.yt_mp3_bytes,
                title=yt_title,
                artist=yt_artist,
                album=yt_album,
                year=yt_year,
                cover_bytes=cover_to_write,
                cover_mime=mime_to_write,
            )
            dl_name = f"{sanitize_filename(yt_title or st.session_state.yt_filename)}.mp3"

            st.download_button(
                label=f"⬇️ Download {dl_name}",
                data=final_yt_bytes,
                file_name=dl_name,
                mime="audio/mpeg",
                type="primary",
                use_container_width=True,
            )

    # --- TAB 2: STANDALONE MP3 ID3v2.3 TAG & COVER ART EDITOR ---
    with tab_edit:
        st.write(
            "Upload an existing local `.mp3` file to inspect and update its ID3v2.3 metadata and cover art."
        )
        uploaded_mp3 = st.file_uploader(
            "Choose an MP3 file", type=["mp3"], key="standalone_mp3_upload"
        )

        if uploaded_mp3 is not None:
            raw_uploaded_bytes = uploaded_mp3.read()
            existing_meta, existing_cover, existing_mime = read_mp3_metadata(
                raw_uploaded_bytes
            )

            c_art2, c_fields2 = st.columns([1, 2])
            with c_art2:
                if existing_cover:
                    st.image(
                        existing_cover,
                        caption="Embedded Artwork",
                        use_container_width=True,
                    )
                else:
                    st.info("No embedded artwork found.")

                replacement_cover = st.file_uploader(
                    "Upload New Cover Art",
                    type=["jpg", "jpeg", "png"],
                    key="standalone_cover_upload",
                )

            with c_fields2:
                edit_title = st.text_input(
                    "Title",
                    value=existing_meta["title"],
                    key=f"ed_title_{uploaded_mp3.name}",
                )
                edit_artist = st.text_input(
                    "Artist",
                    value=existing_meta["artist"],
                    key=f"ed_artist_{uploaded_mp3.name}",
                )
                edit_album = st.text_input(
                    "Album",
                    value=existing_meta["album"],
                    key=f"ed_album_{uploaded_mp3.name}",
                )
                edit_year = st.text_input(
                    "Year",
                    value=existing_meta["year"],
                    key=f"ed_year_{uploaded_mp3.name}",
                )

            final_cover = existing_cover
            final_mime = existing_mime
            if replacement_cover is not None:
                final_cover = replacement_cover.read()
                final_mime = replacement_cover.type or "image/jpeg"

            updated_mp3_bytes = write_mp3_metadata_v23(
                raw_uploaded_bytes,
                title=edit_title,
                artist=edit_artist,
                album=edit_album,
                year=edit_year,
                cover_bytes=final_cover,
                cover_mime=final_mime,
            )

            output_filename = (
                f"{sanitize_filename(edit_title)}.mp3"
                if edit_title.strip()
                else uploaded_mp3.name
            )

            st.download_button(
                label=f"⬇️ Download Updated {output_filename}",
                data=updated_mp3_bytes,
                file_name=output_filename,
                mime="audio/mpeg",
                type="primary",
                use_container_width=True,
            )


# --- 7. ROUTER ---
if st.session_state.authenticated:
    render_main_app()
else:
    render_login_screen()
