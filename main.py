import re
import os
import asyncio
import shutil
from urllib.parse import urlparse, urlunparse

import yt_dlp
from fastapi import FastAPI, Form
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from datetime import datetime, timedelta
import uvicorn

app = FastAPI()

# ===== COOKIES =====
INSTAGRAM_COOKIES = "insta_cookies.txt"
TWITTER_COOKIES   = "twitter_cookies.txt"
FACEBOOK_COOKIES  = "facebook_cookies.txt"
YOUTUBE_COOKIES   = "youtube_cookies.txt"

# ===== FFMPEG CHECK =====
FFMPEG_PATH = shutil.which("ffmpeg")
FFMPEG_EXISTS = FFMPEG_PATH is not None
if FFMPEG_EXISTS:
    print("✅ ffmpeg detected")
else:
    print("⚠️ ffmpeg not found, using single file mode")

# ===== STATE =====
download_queue = asyncio.Queue()
PROGRESS = {}
FILE_PATHS = {}
ERRORS = {}
insta_usage = {}

# ===== UTILITY: Clean YouTube URL =====
def clean_youtube_url(url: str) -> str:
    parsed = urlparse(url)
    # keep only scheme + netloc + path (strip query/params)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))

# ===== DOWNLOAD WORKER =====
async def download_worker():
    while True:
        url, platform, user_id, audio_only = await download_queue.get()
        tmp_file = f"{os.getcwd()}/video_{user_id}.%(ext)s"
        ERRORS[user_id] = None
        PROGRESS[user_id] = 0

        def progress_hook(d):
            if d['status'] == 'downloading':
                percent_str = re.sub(r'\x1b\[[0-9;]*m','', d.get('_percent_str','0')).replace('%','').strip()
                try:
                    PROGRESS[user_id] = int(float(percent_str))
                except:
                    PROGRESS[user_id] = 0
            elif d['status'] == 'finished':
                PROGRESS[user_id] = 100

        def download_video(use_cookies=False, audio_only=False):
            nonlocal url
            # Clean YouTube URL
            if platform == "youtube":
                url = clean_youtube_url(url)

            # ✅ Audio only
            if audio_only and platform == "youtube":
                opts = {
                    'format': 'bestaudio/best',
                    'noplaylist': True,
                    'quiet': True,
                    'no_color': True,
                    'progress_hooks':[progress_hook],
                    'outtmpl': tmp_file,
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                    'force_generic_extractor': True,       # handle Shorts
                    'youtube_include_dash_manifest': False
                }
            else:
                opts = {
                    'format': 'bestvideo+bestaudio/best' if FFMPEG_EXISTS else 'best',
                    'noplaylist': True,
                    'quiet': True,
                    'no_color': True,
                    'progress_hooks':[progress_hook],
                    'outtmpl': tmp_file,
                    'force_generic_extractor': True if platform=="youtube" else False,
                }

            # Cookies
            if use_cookies:
                if platform=="instagram" and os.path.exists(INSTAGRAM_COOKIES):
                    opts['cookiefile'] = INSTAGRAM_COOKIES
                elif platform=="twitter" and os.path.exists(TWITTER_COOKIES):
                    opts['cookiefile'] = TWITTER_COOKIES
                elif platform=="facebook" and os.path.exists(FACEBOOK_COOKIES):
                    opts['cookiefile'] = FACEBOOK_COOKIES
                elif platform=="youtube" and os.path.exists(YOUTUBE_COOKIES):
                    opts['cookiefile'] = YOUTUBE_COOKIES

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                FILE_PATHS[user_id] = ydl.prepare_filename(info)
                return info

        # Try download: without cookies first, then with cookies
        try:
            await asyncio.to_thread(download_video, False, audio_only)
        except Exception:
            try:
                await asyncio.to_thread(download_video, True, audio_only)
            except Exception:
                ERRORS[user_id] = "Wrong platform or video not found."
                PROGRESS[user_id] = 0

        download_queue.task_done()

# ===== STARTUP =====
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(download_worker())

# ===== SERVE INDEX.HTML =====
@app.get("/")
async def home():
    return FileResponse(os.path.join(os.getcwd(), "index.html"), media_type="text/html")

# ===== DOWNLOAD ENDPOINT =====
@app.post("/download/")
async def download_endpoint(
    url: str = Form(...), 
    platform: str = Form(...), 
    user_id: int = Form(...),
    audio_only: bool = Form(False)
):
    # Instagram rate limit
    if platform=="instagram":
        today = datetime.utcnow().date()
        usage = insta_usage.get(user_id, {"count":0,"last_time":None,"day":today})
        if usage["day"] != today:
            usage = {"count":0,"last_time":None,"day":today}
        if usage["count"] >= 10:
            return JSONResponse({"message":"❌ Daily limit reached (10 videos). Try again tomorrow."})
        if usage["last_time"] and datetime.utcnow() - usage["last_time"] < timedelta(minutes=10):
            wait_time = 10 - int((datetime.utcnow() - usage["last_time"]).total_seconds() // 60)
            return JSONResponse({"message":f"⏳ Wait {wait_time} minutes before next download."})

    # Put in queue
    await download_queue.put((url, platform, user_id, audio_only))

    # Wait until file ready or error
    timeout = 300
    waited = 0
    while waited < timeout:
        if ERRORS.get(user_id):
            return JSONResponse({"message": f"❌ {ERRORS[user_id]}"} )
        file_path = FILE_PATHS.get(user_id)
        if file_path and os.path.exists(file_path):
            # Update usage
            if platform=="instagram":
                today = datetime.utcnow().date()
                usage = insta_usage.get(user_id, {"count":0,"last_time":None,"day":today})
                if usage["day"] != today:
                    usage = {"count":0,"last_time":None,"day":today}
                usage["count"] += 1
                usage["last_time"] = datetime.utcnow()
                usage["day"] = today
                insta_usage[user_id] = usage
            return {"file_url": f"/downloaded/{os.path.basename(file_path)}"}
        await asyncio.sleep(1)
        waited +=1
    return {"message":"❌ Failed to download or timeout."}

# ===== PROGRESS ENDPOINT =====
@app.get("/progress/{user_id}")
async def progress_endpoint(user_id: int):
    if ERRORS.get(user_id):
        return {"percent": 0, "error": ERRORS[user_id]}
    return {"percent": PROGRESS.get(user_id,0)}

# ===== SERVE FILE & DELETE AFTER SEND =====
@app.get("/downloaded/{filename}")
async def serve_file(filename: str):
    path = f"{os.getcwd()}/{filename}"
    if os.path.exists(path):
        ext = os.path.splitext(filename)[1].lower()
        media_type = "audio/mpeg" if ext==".mp3" else "video/mp4"
        task = BackgroundTask(delete_file_after_send, path)
        return FileResponse(path, media_type=media_type, filename=filename, background=task)
    return {"error":"File not found"}

async def delete_file_after_send(path):
    await asyncio.sleep(1)
    try:
        os.remove(path)
        user_id = int(re.findall(r'video_(\d+)\.', os.path.basename(path))[0])
        PROGRESS.pop(user_id,None)
        FILE_PATHS.pop(user_id,None)
        ERRORS.pop(user_id,None)
    except:
        pass

# ===== RUN =====
if __name__=="__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
