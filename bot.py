#!/usr/bin/env python3
import os, re, uuid, asyncio, logging, time, subprocess, json, shutil
from pathlib import Path
from urllib.parse import urlparse, quote
import aiohttp, aiofiles
import requests, urllib3
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from ptbcontrib.aiohttp_request import AiohttpRequest
from todus_client import login_with_phone_only, ToDusXMPP

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TODUS_PHONE = os.environ["TODUS_PHONE"]
WORK_DIR = "/tmp/todus_jobs"
STREAM_BUCKET = "https://s3.todus.cu/stream"
PENDING_TTL = 900
MAX_SIZE = 2 * 1024 * 1024 * 1024
os.makedirs(WORK_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

_xmpp = None
_xmpp_lock = asyncio.Lock()
pending = {}
pending_txt = {}
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
PHONE_RE = re.compile(r"^\+?(\d{8,15})$")

# Throttle para no saturar Telegram
class EditState:
    def __init__(self):
        self.last = 0.0
        self.last_text = ""
    def can_edit(self, text, force=False):
        now = time.time()
        if force:
            self.last = now; self.last_text = text; return True
        if now - self.last < 2.0: return False
        if text == self.last_text: return False
        self.last = now; self.last_text = text; return True


def progress_bar(pct, width=15):
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def fmt_size(b):
    if b < 1024: return f"{b} B"
    if b < 1048576: return f"{b/1024:.1f} KB"
    if b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.2f} GB"


def _is_xmpp_alive(x):
    try:
        return x is not None and x._running and x.sock is not None
    except Exception:
        return False


async def get_xmpp():
    global _xmpp
    async with _xmpp_lock:
        if _is_xmpp_alive(_xmpp):
            return _xmpp
        log.info("Login toDus...")
        jwt = await asyncio.to_thread(login_with_phone_only, TODUS_PHONE)
        xmpp = ToDusXMPP(TODUS_PHONE, jwt)
        await asyncio.to_thread(xmpp.start)
        _xmpp = xmpp
        return _xmpp


def _cleanup_pending():
    now = time.time()
    for u in [k for k, v in pending.items() if now - v.get("ts", 0) > PENDING_TTL]:
        pending.pop(u, None)
    for u in [k for k, v in pending_txt.items() if now - v.get("ts", 0) > PENDING_TTL]:
        pending_txt.pop(u, None)


def detect_file_type(url, content_type=""):
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in (".mp4", ".mkv", ".mov", ".avi", ".webm"): return "video"
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"): return "image"
    if ext in (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".opus"): return "audio"
    if content_type.startswith("video/"): return "video"
    if content_type.startswith("image/"): return "image"
    if content_type.startswith("audio/"): return "audio"
    return "document"


def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


async def download_file_with_progress(session, url, dest, on_progress=None):
    headers = {"User-Agent": "Mozilla/5.0"}
    timeout = aiohttp.ClientTimeout(total=None, sock_read=180)
    async with session.get(url, headers=headers, timeout=timeout) as r:
        if r.status >= 400:
            raise RuntimeError(f"HTTP {r.status}")
        total = int(r.headers.get("Content-Length", 0) or 0)
        size = 0
        last_pct = -1
        async with aiofiles.open(dest, "wb") as f:
            async for chunk in r.content.iter_chunked(256 * 1024):
                if chunk:
                    await f.write(chunk)
                    size += len(chunk)
                    if on_progress and total:
                        pct = int(size / total * 100)
                        if pct - last_pct >= 5 or pct == 100:
                            last_pct = pct
                            await on_progress(size, total, pct)
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
        if r.returncode != 0:
            return {}
        info = json.loads(r.stdout or "{}")
        streams = info.get("streams", [])
        fmt = info.get("format", {})
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        duration = 0
        if fmt.get("duration"):
            duration = int(float(fmt["duration"]))
        elif video and video.get("duration"):
            duration = int(float(video["duration"]))
        return {
            "duration": duration,
            "size": int(fmt.get("size", 0)),
            "width": int(video.get("width", 0)) if video else 0,
            "height": int(video.get("height", 0)) if video else 0,
        }
    except Exception:
        return {}


def upload_to_stream_with_progress(data, filename, content_type, on_progress=None):
    """Sube a S3 con progreso (chunked)."""
    prefix = uuid.uuid4().hex[:8]
    object_name = f"{prefix}_{filename}"
    url = f"{STREAM_BUCKET}/{quote(object_name)}"
    total = len(data)
    chunk_size = 1024 * 1024
    sent = 0
    last_pct = -1

    # Usamos requests con un file-like para streaming
    import io
    class ProgressIO(io.BytesIO):
        def read(self, n=-1):
            nonlocal sent, last_pct
            chunk = super().read(n)
            if chunk:
                sent += len(chunk)
                if on_progress and total:
                    pct = int(sent / total * 100)
                    if pct - last_pct >= 5 or pct == 100:
                        last_pct = pct
                        try:
                            on_progress(sent, total, pct)
                        except Exception:
                            pass
            return chunk

    r = requests.put(
        url, data=ProgressIO(data),
        headers={"Content-Type": content_type, "Content-Length": str(total)},
        timeout=600, verify=False,
    )
    r.raise_for_status()
    return url


def send_stanza(xmpp, phone, url, ftype, size, name, meta=None, thumb_url=""):
    msg_id = uuid.uuid4().hex[:16]
    file_id = uuid.uuid4().hex[:16]
    meta = meta or {}
    to = f"{phone}@im.todus.cu"
    url_e = _xml_escape(url)
    name_e = _xml_escape(name)
    thumb_e = _xml_escape(thumb_url) if thumb_url else ""
    d = meta.get("duration", 0)
    w = meta.get("width", 0)
    he = meta.get("height", 0)
    if ftype == "video":
        stanza = (f'<m to="{to}" t="c" i="{msg_id}" xmlns="jc"><k xmlns="x8"/>'
                  f'<video xmlns="video:n" i="{file_id}" mi="{msg_id}" url="{url_e}" '
                  f'n="{name_e}" s="{size}" h="" d="{d}" w="{w}" he="{he}" tnail="{thumb_e}"/><b/></m>')
    elif ftype == "image":
        stanza = (f'<m to="{to}" t="c" i="{msg_id}" xmlns="jc"><k xmlns="x8"/>'
                  f'<image xmlns="image:n" i="{file_id}" mi="{msg_id}" url="{url_e}" '
                  f'n="{name_e}" s="{size}" h="" w="{w}" he="{he}" tnail="{thumb_e}"/><b/></m>')
    elif ftype == "audio":
        stanza = (f'<m to="{to}" t="c" i="{msg_id}" xmlns="jc"><k xmlns="x8"/>'
                  f'<voice xmlns="voice:n" i="{file_id}" mi="{msg_id}" url="{url_e}" '
                  f's="{size}" h="" d="{d}" ws=""/><b/></m>')
    else:
        stanza = (f'<m to="{to}" t="c" i="{msg_id}" xmlns="jc"><k xmlns="x8"/>'
                  f'<file xmlns="file:n" i="{file_id}" mi="{msg_id}" n="{name_e}" '
                  f'url="{url_e}" s="{size}" h=""/><b/></m>')
    xmpp.send_stanza(stanza)
    return msg_id


async def probe_url(session, url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.head(url, headers=headers, timeout=15, allow_redirects=True) as r:
            if r.status < 400:
                return r.headers.get("Content-Type", ""), int(r.headers.get("Content-Length", 0) or 0)
    except Exception:
        pass
    headers["Range"] = "bytes=0-0"
    async with session.get(url, headers=headers, timeout=20, allow_redirects=True) as r:
        if r.status >= 400:
            raise RuntimeError(f"HTTP {r.status}")
        cr = r.headers.get("Content-Range", "")
        size = 0
        if "/" in cr:
            try: size = int(cr.split("/")[-1])
            except ValueError: pass
        if not size: size = int(r.headers.get("Content-Length", 0) or 0)
        return r.headers.get("Content-Type", ""), size


def normalize_phone(text):
    text = text.strip().replace(" ", "").replace("-", "")
    m = PHONE_RE.match(text)
    return m.group(1) if m else None


# ═══════════════════════════════════════════════════════════
# PROCESAR UNA URL CON PROGRESO VISUAL
# ═══════════════════════════════════════════════════════════

async def process_single_with_progress(xmpp, session, url, phone, status_msg,
                                       index, total, on_edit):
    """
    Procesa una URL con barra de progreso visual.
    on_edit(text) — callback para editar el mensaje (con throttle).
    """
    try:
        ct, _ = await probe_url(session, url)
        ftype = detect_file_type(url, ct)

        temp_dir = Path(WORK_DIR) / uuid.uuid4().hex[:8]
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            ext = Path(urlparse(url).path).suffix or ".bin"
            local_file = temp_dir / f"file{ext}"

            # ─── DESCARGA CON PROGRESO ───
            async def on_dl(sent, total, pct):
                txt = (
                    f"📥 Descargando [{index}/{total}]\n"
                    f"[{progress_bar(pct)}] {pct}%\n"
                    f"{fmt_size(sent)}/{fmt_size(total)}"
                )
                await on_edit(txt)
            await on_edit(f"📥 Descargando [{index}/{total}]\nIniciando...", force=True)
            real_size = await download_file_with_progress(session, url, local_file, on_dl)

            # ─── METADATOS ───
            meta = {}
            if ftype in ("video", "audio"):
                await on_edit(f"🔍 Metadatos [{index}/{total}]...", force=True)
                meta = await asyncio.to_thread(run_ffprobe, str(local_file))
            size = meta.get("size") or real_size

            # ─── THUMBNAIL ───
            thumb_url = ""
            if ftype == "video":
                thumb_path = temp_dir / "thumb.jpg"
                ok = await asyncio.to_thread(extract_thumbnail, str(local_file), str(thumb_path))
                if ok:
                    with open(thumb_path, "rb") as f:
                        thumb_data = f.read()
                    await on_edit(f"🖼️ Thumbnail [{index}/{total}]...", force=True)
                    def _up_thumb():
                        return upload_to_stream_with_progress(thumb_data, "thumb.jpg", "image/jpeg")
                    thumb_url = await asyncio.to_thread(_up_thumb)

            # ─── SUBIDA A S3 CON PROGRESO ───
            with open(local_file, "rb") as f:
                data = f.read()
            if ftype == "video": ctype = "video/mp4"
            elif ftype == "image": ctype = "image/jpeg"
            elif ftype == "audio": ctype = "audio/mpeg"
            else: ctype = "application/octet-stream"
            name = Path(urlparse(url).path).name or local_file.name

            loop = asyncio.get_running_loop()
            last_up = {"pct": -1}

            def on_up_sync(sent, total, pct):
                if pct - last_up["pct"] < 5 and pct != 100:
                    return
                last_up["pct"] = pct
                txt = (
                    f"⬆️ Subiendo a S3 [{index}/{total}]\n"
                    f"[{progress_bar(pct)}] {pct}%\n"
                    f"{fmt_size(sent)}/{fmt_size(total)}"
                )
                asyncio.run_coroutine_threadsafe(on_edit(txt), loop)

            await on_edit(f"⬆️ Subiendo a S3 [{index}/{total}]...", force=True)
            def _up_main():
                return upload_to_stream_with_progress(data, name, ctype, on_up_sync)
            get_url = await asyncio.to_thread(_up_main)

            # ─── ENVÍO POR TODUS ───
            await on_edit(f"📤 Enviando por toDus [{index}/{total}]...", force=True)
            msg_id = await asyncio.to_thread(
                send_stanza, xmpp, phone, get_url, ftype, size, name, meta, thumb_url
            )
            return {"ok": True, "url": url, "tipo": ftype, "size": size, "msg_id": msg_id}
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as e:
        log.exception(f"Error procesando {url}")
        return {"ok": False, "url": url, "error": str(e)[:150]}


# ═══════════════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════════════

async def cmd_start(update, context):
    await update.message.reply_text(
        "Bot ToDus\n\n"
        "Envia una URL o un archivo .txt con varias URLs.\n"
        "Luego te pido el numero toDus del destinatario."
    )


async def handle_url(update, context):
    uid = update.effective_user.id
    _cleanup_pending()
    text = (update.message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("URL invalida.")
        return
    url = m.group(0)
    try:
        async with aiohttp.ClientSession() as s:
            ct, size = await probe_url(s, url)
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)[:200]}")
        return
    if size and size > MAX_SIZE:
        await update.message.reply_text(f"Muy grande: {fmt_size(size)}")
        return
    ftype = detect_file_type(url, ct)
    pending[uid] = {"url": url, "file_type": ftype, "size": size, "ts": time.time()}
    await update.message.reply_text(
        f"📎 Tipo: {ftype}\n📊 Tamanio: {fmt_size(size) if size else '?'}\n\n📱 Numero toDus?"
    )


async def handle_document(update, context):
    uid = update.effective_user.id
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("Solo acepto archivos .txt con URLs.")
        return
    status = await update.message.reply_text("📄 Leyendo archivo...")
    try:
        file = await context.bot.get_file(doc.file_id)
        content = await file.download_as_bytearray()
        text = content.decode("utf-8", errors="ignore")
        urls = []
        for line in text.splitlines():
            line = line.strip()
            if not line: continue
            m = URL_RE.search(line)
            if m: urls.append(m.group(0))
        if not urls:
            await status.edit_text("No encontre URLs en el archivo.")
            return
        pending_txt[uid] = {"urls": urls, "chat_id": update.effective_chat.id, "ts": time.time()}
        await status.edit_text(
            f"📄 Encontre {len(urls)} URLs.\n\n📱 Numero toDus destinatario?\n"
            f"Formatos: 5351234567, +5351234567, 51234567"
        )
    except Exception as e:
        log.exception("Error leyendo txt")
        await status.edit_text(f"Error: {str(e)[:200]}")


async def handle_phone(update, context):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # ═══ Procesar .txt pendiente ═══
    if uid in pending_txt:
        phone = normalize_phone(text)
        if not phone:
            await update.message.reply_text("Numero invalido. Ej: 5351234567")
            return
        info = pending_txt.pop(uid)
        urls = info["urls"]
        total = len(urls)
        status = await update.message.reply_text(f"🚀 Iniciando lote de {total} URLs...")

        state = EditState()

        async def on_edit(txt, force=False):
            if state.can_edit(txt, force):
                try:
                    await status.edit_text(txt)
                except Exception:
                    pass

        xmpp = await get_xmpp()
        ok = 0
        fail = 0
        errors = []

        async with aiohttp.ClientSession() as s:
            for i, url in enumerate(urls, 1):
                r = await process_single_with_progress(
                    xmpp, s, url, phone, status, i, total, on_edit
                )
                if r["ok"]:
                    ok += 1
                else:
                    fail += 1
                    errors.append(f"❌ {url[:50]}: {r.get('error','?')[:80]}")

        resumen = (
            f"✅ **Lote completado**\n\n"
            f"📊 Procesadas: {total}\n"
            f"✅ OK: {ok}\n"
            f"❌ Errores: {fail}\n"
            f"📱 Destinatario: {phone}"
        )
        if errors:
            resumen += "\n\n⚠️ **Errores:**\n" + "\n".join(errors[:5])
        await status.edit_text(resumen)
        return

    # ═══ Procesar URL suelta ═══
    if uid in pending:
        phone = normalize_phone(text)
        if not phone:
            await update.message.reply_text("Numero invalido. Ej: 5351234567")
            return
        info = pending.pop(uid)
        url = info["url"]
        status = await update.message.reply_text("🚀 Procesando...")
        state = EditState()

        async def on_edit(txt, force=False):
            if state.can_edit(txt, force):
                try:
                    await status.edit_text(txt)
                except Exception:
                    pass

        xmpp = await get_xmpp()
        async with aiohttp.ClientSession() as s:
            r = await process_single_with_progress(xmpp, s, url, phone, status, 1, 1, on_edit)

        if r["ok"]:
            await status.edit_text(
                f"✅ **Enviado**\n\n"
                f"📎 Tipo: {r['tipo']}\n"
                f"📊 Tamanio: {fmt_size(r['size'])}\n"
                f"📱 Destinatario: {phone}\n"
                f"🆔 Msg ID: `{r['msg_id']}`"
            )
        else:
            await status.edit_text(f"❌ Error: {r.get('error','?')}")
        return

    await handle_url(update, context)


def main():
    global _xmpp
    log.info("Arrancando XMPP persistente...")
    jwt = login_with_phone_only(TODUS_PHONE)
    _xmpp = ToDusXMPP(TODUS_PHONE, jwt)
    _xmpp.start()
    log.info("XMPP listo. Arrancando bot de Telegram...")

    application = (
        Application.builder()
        .token(TG_BOT_TOKEN)
        .request(AiohttpRequest(connection_pool_size=256))
        .get_updates_request(AiohttpRequest())
        .build()
    )
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone))
    log.info("Bot listo")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
