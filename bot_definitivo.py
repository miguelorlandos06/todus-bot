import json, re, threading, os, traceback
import httpx
from todus import ToDusClient2, FileType, util

PHONE = os.getenv("TODUS_PHONE", "5350155246")
BOT_JID = f"{PHONE}@im.todus.cu"
HEADERS = {"User-Agent": "Mozilla/5.0"}

API_BASE = "https://api.apklis.cu"
SEED_SHA256 = "8da7f305e9763e542b087935714b726fafa3e26105e9e6f96261074c56461ce1"

APPS_MAP = {
    "todus": "cu.todus.android",
    "apklis": "cu.uci.android.apklis",
    "picta": "cu.picta.android",
}

client = ToDusClient2(PHONE)
client.login_with_phone_only()

async def get_apklis_data(package_name, keyword):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        r = await http.get(f"{API_BASE}/v2/application/", params={"package_name": package_name})
        r.raise_for_status()
        app_data = r.json().get("results", [])
        if not app_data: return None
        app = app_data[0]
        
        r_rel = await http.get(f"{API_BASE}/v2/release/", params={"package_name": package_name, "limit": 1})
        r_rel.raise_for_status()
        rel_data = r_rel.json().get("results", [])
        if not rel_data: return None
        last = rel_data[0]

        version_name = last.get("version_name", "N/A")
        version_code = last.get("version_code")
        sha256 = last.get("sha256")
        size_bytes = last.get("size", 0)
        
        url = None
        r_url = await http.post(f"{API_BASE}/v2/release/get_url/", json={"release": sha256})
        if r_url.status_code == 200 and r_url.json().get("url"):
            url = r_url.json()["url"]
        else:
            r_gid = await http.post(f"{API_BASE}/v2/release/get_url/", json={"release": SEED_SHA256})
            if r_gid.status_code == 200:
                gid_url = r_gid.json().get("url", "")
                if "?" in gid_url:
                    gid = gid_url.split("?", 1)[1]
                    url = f"https://archive.apklis.cu/application/apk/{package_name}-v{version_code}.apk?{gid}"

        changelog_raw = last.get("changelog", "")
        texto_limpio = re.sub(r"<li[^>]*>", "\n", changelog_raw)
        texto_limpio = re.sub(r"<[^>]+>", "", texto_limpio)
        lineas = [l.strip() for l in texto_limpio.split("\n") if l.strip() and keyword not in l.lower() and "historial" not in l.lower()]
        changelog = "\n\n".join([f"• {l}" for l in lineas]) if lineas else "Sin novedades registradas."

        texto = (
            f"📦 <b>{app.get('name', keyword.capitalize())}</b> | <code>{version_name}</code>\n\n"
            f"📅 <b>Actualizado:</b> <u>{app.get('updated', 'N/A').split('T')[0]}</u>\n\n"
            f"💾 <b>Tamaño:</b> <code>{size_bytes / (1024*1024):.2f} MB</code>\n\n"
            f"📝 <b>NOVEDADES:</b>\n\n"
            f"<pre>{changelog}</pre>\n\n"
            f"🔗 <a href='https://test.apklis.cu/es/applications/{package_name}'>Ver en Apklis</a>"
        )
        
        return {"texto": texto, "icon_url": app.get("icon", ""), "url_apk": url, "version_code": version_code}

def procesar_peticion(grupo_id, target_keyword, pide_apk, pide_info):
    try:
        client.send_chat_state(client.token, util.build_jid(grupo_id), "composing")
        
        import asyncio
        data = asyncio.run(get_apklis_data(APPS_MAP[target_keyword], target_keyword))
        
        if not data:
            client.groups.send_message(grupo_id, f"❌ No encontré datos para <b>{target_keyword}</b> en Apklis.")
            return

        texto = data["texto"]
        icon_url = data["icon_url"]
        
        if pide_apk:
            if not data["url_apk"]:
                client.groups.send_message(grupo_id, "❌ No pude generar un enlace de descarga válido.")
                return
                
            print(f"📦 Descargando APK de {target_keyword}...")
            with httpx.Client(timeout=300, follow_redirects=True) as http:
                req = http.get(data["url_apk"])
                req.raise_for_status()
                apk_bytes = req.content
                
            filename = f"{target_keyword}_{data['version_code']}.apk"
            with open(filename, "wb") as f:
                f.write(apk_bytes)

            print("☁️ Subiendo APK a ToDus...")
            with open(filename, "rb") as f:
                apk_todus_url = client.upload_file(f.read(), file_type=FileType.FILE, file_name=filename)
                
            print("🚀 Enviando APK...")
            client.groups.send_file(grupo_id, apk_todus_url, filename, os.path.getsize(filename), caption=texto)
            os.remove(filename)

        elif pide_info:
            print(f"🖼️ Descargando logo de {target_keyword}...")
            with httpx.Client(timeout=30, follow_redirects=True) as http:
                img_bytes = http.get(icon_url).content
            
            with open("temp_logo.png", "wb") as f:
                f.write(img_bytes)
                
            print("☁️ Subiendo logo...")
            with open("temp_logo.png", "rb") as f:
                img_todus_url = client.upload_file(f.read(), file_type=FileType.PICTURE, file_name="temp_logo.png")
                
            print("🚀 Enviando Info...")
            client.groups.send_image(grupo_id, img_todus_url, "temp_logo.png", os.path.getsize("temp_logo.png"), 200, 200, "L6PZfSi_.AyE_3t7t7R**0o#DgR4", texto)
            os.remove("temp_logo.png")
            
        print("✅ Proceso terminado con éxito.")

    except Exception:
        print("\n❌ ERROR DETALLADO:")
        traceback.print_exc()
        client.groups.send_message(grupo_id, f"❌ Ocurrió un error procesando la petición de {target_keyword}.")

def on_message(msg):
    if msg.get("type") != "gc": return
    raw = msg.get("raw", "")
    body = (msg.get("body") or "").lower()

    if f"ui='{BOT_JID}'" not in raw and f'ui="{BOT_JID}"' not in raw: return

    print(f"\n🔔 Mención detectada: {body}")
    grupo_id = msg.get("from", "").split("@")[0]

    target_keyword = next((k for k in APPS_MAP if k in body), None)
    if not target_keyword:
        threading.Thread(target=lambda: client.groups.send_message(grupo_id, "⚠️ No reconozco esa app. Apps válidas: toDus, Apklis, Picta."), daemon=True).start()
        return

    pide_apk = "apk" in body
    pide_info = "info" in body

    if pide_apk or pide_info:
        threading.Thread(target=procesar_peticion, args=(grupo_id, target_keyword, pide_apk, pide_info), daemon=True).start()

print(f"✅ Bot Definitivo conectado y escuchando en {BOT_JID}...")
try:
    client.listen_messages(on_message)
except TypeError:
    client.listen_messages(client.token, on_message)
