#!/usr/bin/env python3
import os, re, uuid, asyncio, logging, time, subprocess, json, shutil
from pathlib import Path
from urllib.parse import urlparse, quote
import aiohttp, aiofiles
import requests, urllib3
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from ptbcontrib.aiohttp_request import AiohttpRequest
from todus_client import (
    login_with_phone_only, ToDusXMPP,
    reserve_upload_url, upload_to_s3,
    FILE_TYPE_VIDEO, FILE_TYPE_IMAGE, FILE_TYPE_VOICE, FILE_TYPE_DOC,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TODUS_PHONE  = os.environ["TODUS_PHONE"]
WORK_DIR = "/tmp/todus_jobs"
PENDING_TTL = 900
MAX_SIZE = 2 * 1024 * 1024 * 1024
os.makedirs(WORK_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

_xmpp = None
_xmpp_lock = asyncio.Lock()
pending = {}
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

def _is_xmpp_alive(x):
    try: return x is not None and x._running and x.sock is not None
    except Exception: return False

async def get_xmpp():
    global _xmpp
    async with _xmpp_lock:
        if _is_xmpp_alive(_xmpp): return _xmpp
        log.info("Login toDus...")
        jwt = await asyncio.to_thread(login_with_phone_only, TODUS_PHONE)
        xmpp = ToDusXMPP(TODUS_PHONE, jwt)
        await asyncio.to_thread(xmpp.connect)
        _xmpp = xmpp
        return _xmpp

def _cleanup_pending():
    now = time.time()
    for u in [k for k, v in pending.items() if now - v.get("ts", 0) > PENDING_TTL]:
        pending.pop(u, None)

def detect_file_type(url, content_type=""):
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in (".mp4", ".mkv", ".mov", ".avi", ".webm"): return "video"
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"): return "image"
    if ext in (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".opus"): return "audio"
    if content_type.startswith("video/"): return "video"
    if content_type.startswith("image/"): return "image"
    if content_type.startswith("audio/"): return "audio"
    return "document"

def fmt_size(b):
    if b < 1024: return f"{b} B"
    if b < 1048576: return f"{b/1024:.1f} KB"
    if b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.2f} GB"

def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace("\"", "&quot;").replace("\x27", "&apos;"))

async def download_file(session, url, dest):
    headers = {"User-Agent": "Mozilla/5.0"}
    timeout = aiohttp.ClientTimeout(total=None, sock_read=180)
    async with session.get(url, headers=headers, timeout=timeout) as r:
        if r.status >= 400: raise RuntimeError(f"HTTP {r.status}")
        size = 0
        async with aiofiles.open(dest, "wb") as f:
            async for chunk in r.content.iter_chunked(1024 * 1024):
                if chunk: await f.write(chunk); size += len(chunk)
    return size

def extract_thumbnail(video_path, out_path):
    try:
        r = subprocess.run([
            "ffmpeg", "-y", "-ss", "1", "-i", video_path,
            "-vframes", "1", "-vf", "scale=320:-1", out_path,
        ], capture_output=True, timeout=60)
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False

def run_ffprobe(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                            "-show_format", "-show_streams", path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0: return {}
        info = json.loads(r.stdout or "{}")
        streams = info.get("streams", [])
        fmt = info.get("format", {})
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        duration = 0
        if fmt.get("duration"): duration = int(float(fmt["duration"]))
        elif video and video.get("duration"): duration = int(float(video["duration"]))
        return {"duration": duration, "size": int(fmt.get("size", 0)),
                "width": int(video.get("width", 0)) if video else 0,
                "height": int(video.get("height", 0)) if video else 0}
    except Exception: return {}

def upload_and_get_url(xmpp, data, file_type, content_type):
    put_url, get_url = reserve_upload_url(xmpp, len(data), file_type)
    upload_to_s3(put_url, data, content_type)
    return get_url

def send_stanza(xmpp, phone, url, ftype, size, name, meta=None, thumb_url=""):
    msg_id = uuid.uuid4().hex[:16]
    file_id = uuid.uuid4().hex[:16]
    meta = meta or {}
    to = f"{phone}@im.todus.cu"
    url_e = _xml_escape(url)
    name_e = _xml_escape(name)
    thumb_e = _xml_escape(thumb_url) if thumb_url else ""
    d = meta.get("duration", 0); w = meta.get("width", 0); he = meta.get("height", 0)
    if ftype == "video":
        stanza = (f"<m to=\"{to}\" t=\"c\" i=\"{msg_id}\" xmlns=\"jc\"><k xmlns=\"x8\"/>"
                  f"<video xmlns=\"video:n\" i=\"{file_id}\" mi=\"{msg_id}\" url=\"{url_e}\" "
                  f"n=\"{name_e}\" s=\"{size}\" h=\"\" d=\"{d}\" w=\"{w}\" he=\"{he}\" tnail=\"{thumb_e}\"/><b/></m>")
    elif ftype == "image":
        stanza = (f"<m to=\"{to}\" t=\"c\" i=\"{msg_id}\" xmlns=\"jc\"><k xmlns=\"x8\"/>"
                  f"<image xmlns=\"image:n\" i=\"{file_id}\" mi=\"{msg_id}\" url=\"{url_e}\" "
                  f"n=\"{name_e}\" s=\"{size}\" h=\"\" w=\"{w}\" he=\"{he}\" tnail=\"{thumb_e}\"/><b/></m>")
    elif ftype == "audio":
        stanza = (f"<m to=\"{to}\" t=\"c\" i=\"{msg_id}\" xmlns=\"jc\"><k xmlns=\"x8\"/>"
                  f"<voice xmlns=\"voice:n\" i=\"{file_id}\" mi=\"{msg_id}\" url=\"{url_e}\" "
                  f"s=\"{size}\" h=\"\" d=\"{d}\" ws=\"\"/><b/></m>")
    else:
        stanza = (f"<m to=\"{to}\" t=\"c\" i=\"{msg_id}\" xmlns=\"jc\"><k xmlns=\"x8\"/>"
                  f"<file xmlns=\"file:n\" i=\"{file_id}\" mi=\"{msg_id}\" n=\"{name_e}\" "
                  f"url=\"{url_e}\" s=\"{size}\" h=\"\"/><b/></m>")
    xmpp.send_stanza(stanza)
    return msg_id

async def probe_url(session, url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.head(url, headers=headers, timeout=15, allow_redirects=True) as r:
            if r.status < 400:
                return r.headers.get("Content-Type", ""), int(r.headers.get("Content-Length", 0) or 0)
    except Exception: pass
    headers["Range"] = "bytes=0-0"
    async with session.get(url, headers=headers, timeout=20, allow_redirects=True) as r:
        if r.status >= 400: raise RuntimeError(f"HTTP {r.status}")
        cr = r.headers.get("Content-Range", "")
        size = 0
        if "/" in cr:
            try: size = int(cr.split("/")[-1])
            except ValueError: pass
        if not size: size = int(r.headers.get("Content-Length", 0) or 0)
        return r.headers.get("Content-Type", ""), size

async def cmd_start(update, context):
    await update.message.reply_text("🔗 Bot ToDus\n\nEnvía una URL y luego el número toDus del destinatario.")

async def cmd_status(update, context):
    alive = _is_xmpp_alive(_xmpp)
    await update.message.reply_text("Estado XMPP: " + ("OK" if alive else "NO") + " | Telefono: " + TODUS_PHONE)

async def handle_url(update, context):
    uid = update.effective_user.id
    _cleanup_pending()
    text = (update.message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("URL inválida."); return
    url = m.group(0)
    try:
        async with aiohttp.ClientSession() as s:
            ct, size = await probe_url(s, url)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}"); return
    if size and size > MAX_SIZE:
        await update.message.reply_text(f"⚠️ Muy grande: {fmt_size(size)}"); return
    ftype = detect_file_type(url, ct)
    pending[uid] = {"url": url, "file_type": ftype, "size": size, "ts": time.time()}
    await update.message.reply_text(f"📎 Tipo: {ftype}\n📊 Tamaño: {fmt_size(size) if size else ?}\n\n¿A qué número toDus?")

async def handle_phone(update, context):
    uid = update.effective_user.id
    if uid not in pending: return
    phone = (update.message.text or "").strip().replace("+", "").replace(" ", "").replace("-", "")
    if not phone.isdigit() or len(phone) < 8:
        await update.message.reply_text("⚠️ Número inválido."); return
    info = pending.pop(uid)
    url = info["url"]; ftype = info["file_type"]
    status = await update.message.reply_text(f"⏳ Procesando {ftype}...")
    temp_dir = Path(WORK_DIR) / uuid.uuid4().hex[:8]
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        await status.edit_text("⬇️ Descargando...")
        ext = Path(urlparse(url).path).suffix or ".bin"
        local_file = temp_dir / f"file{ext}"
        async with aiohttp.ClientSession() as s:
            real_size = await download_file(s, url, local_file)
        meta = {}
        if ftype in ("video", "audio"):
            await status.edit_text("🔍 Metadata (ffprobe)...")
            meta = await asyncio.to_thread(run_ffprobe, str(local_file))
        size = meta.get("size") or real_size
        xmpp = await get_xmpp()
        thumb_url = ""
        if ftype == "video":
            await status.edit_text("🖼️ Extrayendo thumbnail...")
            thumb_path = temp_dir / "thumb.jpg"
            ok = await asyncio.to_thread(extract_thumbnail, str(local_file), str(thumb_path))
            if ok:
                with open(thumb_path, "rb") as f: thumb_data = f.read()
                await status.edit_text("⬆️ Subiendo thumbnail...")
                def _up_thumb():
                    return upload_and_get_url(xmpp, thumb_data, FILE_TYPE_IMAGE, "image/jpeg")
                thumb_url = await asyncio.to_thread(_up_thumb)
                log.info(f"Thumb: {thumb_url}")
        await status.edit_text("⬆️ Subiendo archivo a S3...")
        with open(local_file, "rb") as f: data = f.read()
        if ftype == "video":
            fcode, ctype = FILE_TYPE_VIDEO, "video/mp4"
        elif ftype == "image":
            fcode, ctype = FILE_TYPE_IMAGE, "image/jpeg"
        elif ftype == "audio":
            fcode, ctype = FILE_TYPE_VOICE, "audio/mpeg"
        else:
            fcode, ctype = FILE_TYPE_DOC, "application/octet-stream"
        def _up_main():
            return upload_and_get_url(xmpp, data, fcode, ctype)
        get_url = await asyncio.to_thread(_up_main)
        await status.edit_text("📤 Enviando mensaje...")
        name = Path(urlparse(url).path).name or local_file.name
        msg_id = await asyncio.to_thread(send_stanza, xmpp, phone, get_url, ftype, size, name, meta, thumb_url)
        resumen = f"✅ Enviado\n📎 {ftype}\n📊 {fmt_size(size)}\n"
        if meta.get("duration"): resumen += f"🎬 {meta[\x27duration\x27]}s\n"
        if meta.get("width") and meta.get("height"): resumen += f"📐 {meta[\x27width\x27]}x{meta[\x27height\x27]}\n"
        if thumb_url: resumen += f"🖼️ Thumb: subido\n"
        resumen += f"📱 {phone}\n🆔 {msg_id}"
        await status.edit_text(resumen)
    except Exception as e:
        log.exception("Error")
        try: await status.edit_text(f"❌ Error: {str(e)[:300]}")
        except Exception: pass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

async def handle_text(update, context):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if uid in pending:
        digits = text.replace("+", "").replace(" ", "").replace("-", "")
        if digits.isdigit() and len(digits) >= 8:
            await handle_phone(update, context); return
        if URL_RE.search(text):
            await handle_url(update, context); return
        await update.message.reply_text("⚠️ Número toDus válido o nueva URL."); return
    if URL_RE.search(text): await handle_url(update, context)
    else: await update.message.reply_text("🔗 Envíame una URL.")

def main():
    application = (Application.builder().token(TG_BOT_TOKEN)
        .request(AiohttpRequest(connection_pool_size=256))
        .get_updates_request(AiohttpRequest()).build())
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot listo")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__": main()
