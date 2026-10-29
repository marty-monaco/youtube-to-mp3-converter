import os
import glob
import tempfile
import streamlit as st
import yt_dlp

st.set_page_config(page_title="YouTube to MP3", page_icon="🎵", layout="centered")

st.title("🎵 YouTube to MP3 Converter")
st.write("Extract audio tracks with embedded ID3 metadata and album cover art.")

url = st.text_input("YouTube URL:", placeholder="https://www.youtube.com/watch?v=...")
bitrate = st.selectbox(
    "Target Audio Bitrate:",
    options=["128", "192", "256", "320"],
    index=1,
    help="Higher bitrates preserve more audio detail at the cost of a slightly larger file size."
)

if "converted_file" not in st.session_state:
    st.session_state.converted_file = None
if "download_name" not in st.session_state:
    st.session_state.download_name = ""

def process_youtube_audio(video_url: str, quality: str):
    temp_dir = tempfile.mkdtemp()
    # yt-dlp uses %(ext)s during the process and replaces it with mp3 after conversion
    out_template = os.path.join(temp_dir, "%(title)s.%(ext)s")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_template,
        # Instruct yt-dlp to download the highest-resolution thumbnail available
        'writethumbnail': True,
        'postprocessors': [
            # 1. Extract audio and transcode to MP3
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': quality,
            },
            # 2. Embed video title, uploader/artist, and date into standard ID3 tags
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            },
            # 3. Convert thumbnail (often WebP) to JPEG/PNG and embed as album cover
            {
                'key': 'EmbedThumbnail',
                'already_have_thumbnail': False,
            },
        ],
        'quiet': True,
        'no_warnings': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        title = info.get('title', 'audio')
        
    # Locate the final MP3 file in the isolated temp directory
    mp3_files = glob.glob(os.path.join(temp_dir, "*.mp3"))
    if not mp3_files:
        raise RuntimeError("Audio extraction failed to produce an MP3 file.")
    
    return mp3_files[0], f"{title}.mp3"

if st.button("Convert to MP3", type="primary"):
    if not url.strip():
        st.warning("Please provide a valid YouTube URL.")
    else:
        try:
            with st.spinner("Downloading audio, fetching thumbnail, and writing ID3 tags..."):
                file_path, display_name = process_youtube_audio(url.strip(), bitrate)
                
                # Load file into memory for instant browser download
                with open(file_path, "rb") as f:
                    st.session_state.converted_file = f.read()
                st.session_state.download_name = display_name
                
            st.success("Conversion complete with embedded artwork and tags!")
        except Exception as e:
            st.error(f"Error processing video: {e}")

# Render download button if conversion was successful
if st.session_state.converted_file:
    st.download_button(
        label=f"⬇️ Download {st.session_state.download_name}",
        data=st.session_state.converted_file,
        file_name=st.session_state.download_name,
        mime="audio/mpeg"
    )
