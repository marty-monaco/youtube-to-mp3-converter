import os
import glob
import tempfile
import streamlit as st
import yt_dlp

st.set_page_config(page_title="YouTube to MP3", page_icon="🎵", layout="centered")

st.title("🎵 YouTube to MP3 Converter")
st.write("Extract audio tracks from YouTube videos and download them as MP3 files.")

# Input controls
url = st.text_input("YouTube URL:", placeholder="https://www.youtube.com/watch?v=...")
bitrate = st.selectbox(
    "Target Audio Bitrate:",
    options=["128", "192", "256", "320"],
    index=1,
    help="Higher bitrates yield better audio fidelity but larger file sizes."
)

if "converted_file" not in st.session_state:
    st.session_state.converted_file = None
if "download_name" not in st.session_state:
    st.session_state.download_name = ""

def process_youtube_audio(video_url: str, quality: str):
    temp_dir = tempfile.mkdtemp()
    out_template = os.path.join(temp_dir, "%(title)s.%(ext)s")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': quality,
        }],
        'quiet': True,
        'no_warnings': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        title = info.get('title', 'audio')
        
    # Locate the generated MP3 in the temp directory
    mp3_files = glob.glob(os.path.join(temp_dir, "*.mp3"))
    if not mp3_files:
        raise RuntimeError("Audio extraction failed to produce an MP3 file.")
    
    return mp3_files[0], f"{title}.mp3"

if st.button("Convert to MP3", type="primary"):
    if not url.strip():
        st.warning("Please provide a valid YouTube URL.")
    else:
        try:
            with st.spinner("Extracting audio stream and encoding MP3..."):
                file_path, display_name = process_youtube_audio(url.strip(), bitrate)
                
                # Read into memory so it can be served to the browser
                with open(file_path, "rb") as f:
                    st.session_state.converted_file = f.read()
                st.session_state.download_name = display_name
                
            st.success("Conversion complete! Click below to download your audio file.")
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
