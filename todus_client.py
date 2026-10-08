#!/usr/bin/env python3
"""Cliente XMPP persistente con hilos dedicados y reconexión automática."""
import os, re, ssl, time, uuid, socket, base64, hashlib, logging, threading, queue
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

AUTH_URL = "https://auth.todus.cu/v2/auth/token"
XMPP_HOST = "ws.todus.cu"
XMPP_PORT = 5222
XMPP_DOMAIN = "im.todus.cu"
FAKE_SECRET = "fake1234567890abcdef12345678"
KEEPALIVE_INTERVAL = 25
RECV_BUFFER = 65536
RECONNECT_DELAY = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("todus")

def _varint(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        if n: out += bytes([b | 0x80])
        else: out += bytes([b]); break
    return out

def _sf(num, val):
    if isinstance(val, str): val = val.encode("utf-8")
    return bytes([(num << 3) | 2]) + _varint(len(val)) + val

def login_with_phone_only(phone):
    phone = re.sub(r"[^\d]", "", phone)
    payload = _sf(1, phone) + _sf(2, FAKE_SECRET)
    r = requests.post(AUTH_URL, data=payload,
        headers={"Content-Type": "application/x-protobuf", "User-Agent": "ToDus 2.1.2 Auth"},
        timeout=30, verify=False)
    r.raise_for_status()
    text = r.content.decode("utf-8", errors="ignore")
    m = re.search(r"eyJ[\w\-\.]+", text)
    if not m: raise RuntimeError(f"No JWT: {r.content[:200]}")
    return m.group(0)


class ToDusXMPP:
    """Cliente XMPP persistente: el socket se mantiene vivo en su propio hilo."""
    def __init__(self, phone, jwt):
        self.phone = re.sub(r"[^\d]", "", phone)
        self.jwt = jwt
        self.sock = None
        self.jid = f"{self.phone}@{XMPP_DOMAIN}"
        self.resource = hashlib.md5(self.phone.encode()).hexdigest() + "_Android"
        self.full_jid = f"{self.jid}/{self.resource}"
        self._buffer = b""
        self._running = False
        self._connected = threading.Event()
        self._iq_responses = {}
        self._iq_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stanza_handlers = []
        self._send_queue = queue.Queue()

    # ─── Ciclo de vida ───
    def start(self):
        """Arranca el cliente en background. Bloquea hasta que conecte."""
        self._running = True
        threading.Thread(target=self._manager_loop, daemon=True, name="xmpp-manager").start()
        if not self._connected.wait(timeout=30):
            raise RuntimeError("No conectó en 30s")
        log.info("XMPP listo")

    def stop(self):
        self._running = False
        self._safe_close()

    # ─── Hilo manager: gestiona conexión + reconexión ───
    def _manager_loop(self):
        while self._running:
            try:
                self._connect_once()
                self._connected.set()
                # El reader loop corre aquí mismo hasta que el socket muera
                self._reader_loop()
            except Exception as e:
                log.warning(f"Conexión perdida: {e}")
            finally:
                self._connected.clear()
                self._safe_close()
            if self._running:
                log.info(f"Reconectando en {RECONNECT_DELAY}s...")
                time.sleep(RECONNECT_DELAY)

    def _connect_once(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((XMPP_HOST, XMPP_PORT), timeout=30)
        self.sock = ctx.wrap_socket(raw, server_hostname=XMPP_HOST)
        self.sock.settimeout(30)

        self._send_raw(f'<?xml version="1.0"?><stream:stream to="{XMPP_DOMAIN}" xmlns="jc" xmlns:stream="x1" version="1.0">')
        self._recv_until_features()
        auth_data = b"\x00" + self.phone.encode() + b"\x00" + self.jwt.encode()
        auth_b64 = base64.b64encode(auth_data).decode()
        self._send_raw(f'<auth mechanism="PLAIN" xmlns="urn:ietf:params:xml:ns:xmpp-sasl">{auth_b64}</auth>')
        resp = self._recv_until(b"<ok", timeout=10)
        if b"<ok" not in resp and b"<success" not in resp:
            raise RuntimeError(f"SASL falló: {resp[:300]}")
        log.info("SASL OK")

        self._send_raw(f'<?xml version="1.0"?><stream:stream to="{XMPP_DOMAIN}" xmlns="jc" xmlns:stream="x1" version="1.0">')
        self._recv_until_features()
        bind_id = uuid.uuid4().hex
        self._send_raw(f'<iq type="set" id="{bind_id}"><bind xmlns="urn:ietf:params:xml:ns:xmpp-bind"><resource>{self.resource}</resource></bind></iq>')
        resp = self._recv_until(b"</iq>", timeout=10)
        m = re.search(rb"<jid>([^<]+)</jid>", resp)
        if m:
            self.full_jid = m.group(1).decode()
            log.info(f"Bind: {self.full_jid}")

        self._send_raw('<iq type="set" id="sess1"><session xmlns="urn:ietf:params:xml:ns:xmpp-session"/></iq>')
        self._recv_until(b"</iq>", timeout=10)
        self._send_raw("<presence/>")
        time.sleep(0.3)

        # Arrancar hilos auxiliares (solo una vez por conexión)
        threading.Thread(target=self._keepalive_loop, daemon=True, name="xmpp-keepalive").start()
        threading.Thread(target=self._send_loop, daemon=True, name="xmpp-send").start()

    def _safe_close(self):
        if self.sock:
            try: self._send_raw("</stream:stream>")
            except Exception: pass
            try: self.sock.shutdown(socket.SHUT_RDWR)
            except Exception: pass
            try: self.sock.close()
            except Exception: pass
            self.sock = None

    # ─── Hilo reader (corre dentro del manager) ───
    def _reader_loop(self):
        self.sock.settimeout(1)
        while self._running and self.sock:
            try:
                chunk = self.sock.recv(RECV_BUFFER)
                if not chunk:
                    raise ConnectionError("Socket cerrado por el servidor")
                self._buffer += chunk
                self._process_buffer()
            except socket.timeout:
                continue
            except Exception as e:
                raise ConnectionError(f"Reader: {e}")

    # ─── Hilo keepalive ───
    def _keepalive_loop(self):
        while self._running and self.sock:
            time.sleep(KEEPALIVE_INTERVAL)
            if self._running and self.sock:
                try:
                    self._send_raw(" ")
                except Exception:
                    break

    # ─── Hilo send: envía stanzas desde la cola ───
    def _send_loop(self):
        while self._running and self.sock:
            try:
                stanza = self._send_queue.get(timeout=1)
                if stanza is None:
                    break
                self._send_raw(stanza)
            except queue.Empty:
                continue
            except Exception:
                break

    # ─── API pública ───
    def send_stanza(self, stanza):
        """Encola una stanza para envío (no bloquea)."""
        if not self._connected.is_set():
            raise RuntimeError("XMPP no conectado")
        self._send_queue.put(stanza)

    def send_iq_and_wait(self, iq, timeout=10):
        id_m = re.search(r'id="([^"]+)"', iq)
        if not id_m: raise ValueError("IQ sin id")
        iq_id = id_m.group(1)
        with self._iq_lock:
            self._iq_responses[iq_id] = None
        try:
            self.send_stanza(iq)
            start = time.time()
            while time.time() - start < timeout:
                with self._iq_lock:
                    val = self._iq_responses.get(iq_id)
                if val: return val
                time.sleep(0.05)
            raise TimeoutError(f"IQ {iq_id} sin respuesta")
        finally:
            with self._iq_lock:
                self._iq_responses.pop(iq_id, None)

    def on_message(self, callback):
        self._stanza_handlers.append(callback)

    # ─── Internos ───
    def _send_raw(self, data):
        if not self.sock:
            raise RuntimeError("Socket no inicializado")
        with self._send_lock:
            self.sock.sendall(data.encode("utf-8") if isinstance(data, str) else data)

    def _recv_until(self, marker, timeout=10):
        if not self.sock: return b""
        self.sock.settimeout(timeout)
        data = b""
        try:
            while marker not in data:
                chunk = self.sock.recv(RECV_BUFFER)
                if not chunk: break
                data += chunk
        except socket.timeout: pass
        return data

    def _recv_until_features(self, timeout=10):
        return self._recv_until(b"</stream:features>", timeout=timeout)

    def _process_buffer(self):
        while True:
            text = self._buffer.decode("utf-8", errors="replace")
            start = self._find_stanza_start(text)
            if start == -1:
                self._buffer = b""
                return
            if start > 0:
                self._buffer = self._buffer[start:]
                text = text[start:]
            end = self._find_stanza_end(text)
            if end == -1: return
            stanza = text[:end]
            try: self._buffer = self._buffer[end:].lstrip(b"\n\r ")
            except Exception: self._buffer = b""
            self._handle_stanza(stanza)

    @staticmethod
    def _find_stanza_start(text):
        tags = ("<iq", "<message", "<presence", "<m ", "<m>", "<stream:stream", "<stream:error")
        best = -1
        for t in tags:
            i = text.find(t)
            if i != -1 and (best == -1 or i < best): best = i
        return best

    @staticmethod
    def _find_stanza_end(text):
        m = re.match(r"<([a-zA-Z][\w:\-]*)\b[^>]*/>", text)
        if m: return m.end()
        root_m = re.match(r"<([a-zA-Z][\w:\-]*)", text)
        if not root_m: return -1
        root = root_m.group(1)
        depth = 0; i = 0; n = len(text)
        while i < n:
            if text[i] != "<": i += 1; continue
            if text.startswith("<!--", i):
                j = text.find("-->", i + 4)
                if j == -1: return -1
                i = j + 3; continue
            if text.startswith("<![CDATA[", i):
                j = text.find("]]>", i + 9)
                if j == -1: return -1
                i = j + 3; continue
            j = text.find(">", i)
            if j == -1: return -1
            tag_content = text[i + 1:j]
            self_close = tag_content.endswith("/")
            is_close = tag_content.startswith("/")
            name_m = re.match(r"/?([a-zA-Z][\w:\-]*)", tag_content)
            if not name_m: i = j + 1; continue
            name = name_m.group(1)
            if is_close:
                depth -= 1
                if depth == 0 and name == root: return j + 1
            elif not self_close: depth += 1
            i = j + 1
        return -1

    def _handle_stanza(self, stanza):
        id_m = re.search(r'<iq[^>]*\bid="([^"]+)"', stanza)
        if id_m:
            with self._iq_lock:
                if id_m.group(1) in self._iq_responses:
                    self._iq_responses[id_m.group(1)] = stanza
                    return
        if stanza.startswith("<m ") or stanza.startswith("<message"):
            for handler in list(self._stanza_handlers):
                try: handler(stanza)
                except Exception: pass


FILE_TYPE_DOC = "0"
FILE_TYPE_VOICE = "1"
FILE_TYPE_AUDIO = "2"
FILE_TYPE_VIDEO = "3"
FILE_TYPE_IMAGE = "4"
FILE_TYPE_PROFILE = "5"
FILE_TYPE_PROFILE_THUMB = "6"

def reserve_upload_url(xmpp, size, file_type=FILE_TYPE_VIDEO, room=""):
    iq_id = uuid.uuid4().hex
    iq = f'<iq type="get" id="{iq_id}"><query xmlns="todus:purl" type="{file_type}" persistent="true" size="{size}" room="{room}"/></iq>'
    resp = xmpp.send_iq_and_wait(iq, timeout=15)
    put_m = re.search(r'put="([^"]+)"', resp)
    get_m = re.search(r'get="([^"]+)"', resp)
    if not put_m or not get_m: raise RuntimeError(f"PUrl sin put/get: {resp[:400]}")
    return put_m.group(1), get_m.group(1)

def upload_to_s3(put_url, data, content_type="application/octet-stream"):
    r = requests.put(put_url, data=data,
        headers={"Content-Type": content_type, "Content-Length": str(len(data))},
        timeout=600, verify=False)
    r.raise_for_status()
