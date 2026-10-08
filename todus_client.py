#!/usr/bin/env python3
import os, re, ssl, time, uuid, socket, base64, hashlib, logging, threading
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

AUTH_URL = "https://auth.todus.cu/v2/auth/token"
XMPP_HOST = "ws.todus.cu"
XMPP_PORT = 5222
XMPP_DOMAIN = "im.todus.cu"
FAKE_UUID = "fake-1234-5678-90ab-cdef12345678"
FAKE_SECRET = FAKE_UUID.replace("-", "")[:32]
KEEPALIVE_INTERVAL = 25
RECV_BUFFER = 65536

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("todus")

def _varint(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        if n: out += bytes([b | 0x80])
        else: out += bytes([b]); break
    return out

def _sf(field_num, value):
    if isinstance(value, str): value = value.encode("utf-8")
    tag = bytes([(field_num << 3) | 2])
    return tag + _varint(len(value)) + value

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
    def __init__(self, phone, jwt):
        self.phone = re.sub(r"[^\d]", "", phone)
        self.jwt = jwt
        self.sock = None
        self.jid = f"{self.phone}@{XMPP_DOMAIN}"
        self.resource = hashlib.md5(self.phone.encode()).hexdigest() + "_Android"
        self.full_jid = f"{self.jid}/{self.resource}"
        self._buffer = b""
        self._running = False
        self._iq_responses = {}
        self._iq_lock = threading.Lock()
        self._stanza_handlers = []
        self._send_lock = threading.Lock()

    def connect(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((XMPP_HOST, XMPP_PORT), timeout=30)
        self.sock = ctx.wrap_socket(raw, server_hostname=XMPP_HOST)
        self.sock.settimeout(30)
        self._send_raw(f"<?xml version=\"1.0\"?><stream:stream to=\"{XMPP_DOMAIN}\" xmlns=\"jc\" xmlns:stream=\"x1\" version=\"1.0\">")
        self._recv_until_features()
        auth_data = b"\x00" + self.phone.encode() + b"\x00" + self.jwt.encode()
        auth_b64 = base64.b64encode(auth_data).decode()
        self._send_raw(f"<auth mechanism=\"PLAIN\" xmlns=\"urn:ietf:params:xml:ns:xmpp-sasl\">{auth_b64}</auth>")
        resp = self._recv_until(b"<ok", timeout=10)
        if b"<ok" not in resp and b"<success" not in resp:
            raise RuntimeError(f"SASL falló: {resp[:500]}")
        log.info("SASL PLAIN OK")
        self._send_raw(f"<?xml version=\"1.0\"?><stream:stream to=\"{XMPP_DOMAIN}\" xmlns=\"jc\" xmlns:stream=\"x1\" version=\"1.0\">")
        self._recv_until_features()
        bind_id = uuid.uuid4().hex
        self._send_raw(f"<iq type=\"set\" id=\"{bind_id}\"><bind xmlns=\"urn:ietf:params:xml:ns:xmpp-bind\"><resource>{self.resource}</resource></bind></iq>")
        resp = self._recv_until(b"</iq>", timeout=10)
        m = re.search(rb"<jid>([^<]+)</jid>", resp)
        if m:
            self.full_jid = m.group(1).decode()
            log.info(f"Bind OK: {self.full_jid}")
        self._send_raw("<iq type=\"set\" id=\"sess1\"><session xmlns=\"urn:ietf:params:xml:ns:xmpp-session\"/></iq>")
        self._recv_until(b"</iq>", timeout=10)
        self._send_raw("<presence/>")
        time.sleep(0.3)
        log.info("XMPP conectado")
        self._running = True
        threading.Thread(target=self._reader_loop, daemon=True).start()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    def _send_raw(self, data):
        if not self.sock: raise RuntimeError("Socket no inicializado")
        with self._send_lock:
            try: self.sock.sendall(data.encode("utf-8") if isinstance(data, str) else data)
            except (BrokenPipeError, OSError) as e:
                self._running = False
                raise RuntimeError(f"Envío falló: {e}")

    def send_stanza(self, stanza):
        if not self._running: raise RuntimeError("XMPP no conectado")
        self._send_raw(stanza)

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

    def _reader_loop(self):
        self.sock.settimeout(1)
        while self._running:
            try:
                chunk = self.sock.recv(RECV_BUFFER)
                if not chunk: break
                self._buffer += chunk
                self._process_buffer()
            except socket.timeout: continue
            except Exception: break
        self._running = False

    def _process_buffer(self):
        while True:
            text = self._buffer.decode("utf-8", errors="replace")
            start = self._find_stanza_start(text)
            if start == -1:
                self._buffer = self._buffer.lstrip()
                if not self._buffer.startswith(b"<"):
                    idx = self._buffer.find(b"<")
                    if idx == -1: self._buffer = b""; return
                    self._buffer = self._buffer[idx:]
                return
            if start > 0: self._buffer = self._buffer[start:]; text = text[start:]
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
        id_m = re.search(r"<iq[^>]*\bid=\"([^\"]+)\"", stanza)
        if id_m:
            with self._iq_lock:
                if id_m.group(1) in self._iq_responses:
                    self._iq_responses[id_m.group(1)] = stanza
                    return
        if stanza.startswith("<m ") or stanza.startswith("<message"):
            for handler in list(self._stanza_handlers):
                try: handler(stanza)
                except Exception: pass

    def _keepalive_loop(self):
        while self._running:
            time.sleep(KEEPALIVE_INTERVAL)
            if self._running and self.sock:
                try: self._send_raw(" ")
                except Exception: break

    def send_iq_and_wait(self, iq, timeout=10):
        id_m = re.search(r"id=\"([^\"]+)\"", iq)
        if not id_m: raise ValueError("IQ sin id")
        iq_id = id_m.group(1)
        with self._iq_lock: self._iq_responses[iq_id] = None
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
            with self._iq_lock: self._iq_responses.pop(iq_id, None)

    def on_message(self, callback): self._stanza_handlers.append(callback)

    def close(self):
        self._running = False
        if self.sock:
            try: self._send_raw("</stream:stream>")
            except Exception: pass
            try: self.sock.shutdown(socket.SHUT_RDWR)
            except Exception: pass
            try: self.sock.close()
            except Exception: pass
            self.sock = None

FILE_TYPE_DOC = "0"
FILE_TYPE_VOICE = "1"
FILE_TYPE_AUDIO = "2"
FILE_TYPE_VIDEO = "3"
FILE_TYPE_IMAGE = "4"
FILE_TYPE_PROFILE = "5"
FILE_TYPE_PROFILE_THUMB = "6"

def reserve_upload_url(xmpp, size, file_type=FILE_TYPE_VIDEO, room=""):
    iq_id = uuid.uuid4().hex
    iq = f"<iq type=\"get\" id=\"{iq_id}\"><query xmlns=\"todus:purl\" type=\"{file_type}\" persistent=\"true\" size=\"{size}\" room=\"{room}\"/></iq>"
    resp = xmpp.send_iq_and_wait(iq, timeout=15)
    put_m = re.search(r"put=\"([^\"]+)\"", resp)
    get_m = re.search(r"get=\"([^\"]+)\"", resp)
    if not put_m or not get_m: raise RuntimeError(f"PUrl sin put/get: {resp[:400]}")
    return put_m.group(1), get_m.group(1)

def upload_to_s3(put_url, data, content_type="application/octet-stream"):
    r = requests.put(put_url, data=data,
        headers={"Content-Type": content_type, "Content-Length": str(len(data))},
        timeout=600, verify=False)
    r.raise_for_status()
