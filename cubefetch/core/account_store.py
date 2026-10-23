"""账号本地存储：Windows DPAPI 加密 + HMAC-SHA256 完整性校验。

契约见 ``docs/CONTRACTS.md`` §9 account_store。要点：

- 默认路径 ``appdata_dir()/account.dat``。
- Windows 下账号 JSON 用 DPAPI（``crypt32.CryptProtectData`` /
  ``CryptUnprotectData``，``CRYPTPROTECT_UI_FORBIDDEN``）加密；加密绑定当前
  Windows 用户，文件复制到其他机器/用户下无法解密。
- 文件外层是 JSON 信封 ``{"magic","version","mode","insecure","mac","data"}``。
  ``mac`` 是 HMAC-SHA256，密钥由 ``security.machine_id()`` + 内置盐派生，
  覆盖 ``mode`` 与密文/明文 payload：任何改动、跨机复制、降级改写都会被发现。
- 非 Windows（或 DPAPI 不可用）回退为明文 + HMAC，信封 ``insecure=true``，
  读写时记录警告。
- 读取时任何损坏/篡改/解密失败：返回 ``None`` 并删除坏文件，绝不抛异常。
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import hmac
import json
import logging
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from ..config import appdata_dir
from .auth import Account
from .util import atomic_write_bytes, now

log = logging.getLogger("cubefetch.account_store")

ACCOUNT_FILE = "account.dat"
MAGIC = "CFACCT1"
FORMAT_VERSION = 1
#: 存储格式域分离盐；修改会导致旧账号文件失效（用户需重新登录），不要随意改。
STORE_SALT = b"CubeFetch/account-store/v1|7d3a1f0c9b4e2a68"
#: DPAPI 选项：不弹任何 UI（服务/后台线程中也能工作）
CRYPTPROTECT_UI_FORBIDDEN = 0x01
_DPAPI_DESCRIPTION = "CubeFetch account"


# ====================================================================== DPAPI
class _DataBlob(ctypes.Structure):
    """Win32 ``DATA_BLOB``。"""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


_DPAPI: dict[str, Any] | None = None
_DPAPI_TRIED = False


def _load_dpapi() -> dict[str, Any] | None:
    """加载并配置 crypt32 / kernel32（仅 Windows）；失败返回 None。"""
    global _DPAPI, _DPAPI_TRIED
    if _DPAPI is not None or _DPAPI_TRIED:
        return _DPAPI
    _DPAPI_TRIED = True
    if sys.platform != "win32":
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_p = ctypes.POINTER(_DataBlob)
        crypt32.CryptProtectData.argtypes = [
            blob_p,  # pDataIn
            wintypes.LPCWSTR,  # szDataDescr
            blob_p,  # pOptionalEntropy
            ctypes.c_void_p,  # pvReserved
            ctypes.c_void_p,  # pPromptStruct
            wintypes.DWORD,  # dwFlags
            blob_p,  # pDataOut
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            blob_p,  # pDataIn
            ctypes.c_void_p,  # ppszDataDescr
            blob_p,  # pOptionalEntropy
            ctypes.c_void_p,  # pvReserved
            ctypes.c_void_p,  # pPromptStruct
            wintypes.DWORD,  # dwFlags
            blob_p,  # pDataOut
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
    except (OSError, AttributeError) as exc:  # pragma: no cover - 仅异常 Windows
        log.warning("DPAPI 不可用，将回退明文存储：%s", exc)
        return None
    _DPAPI = {"crypt32": crypt32, "kernel32": kernel32}
    return _DPAPI


def dpapi_available() -> bool:
    """当前环境是否可使用 Windows DPAPI。"""
    return _load_dpapi() is not None


def _make_blob(data: bytes) -> tuple[_DataBlob, Any]:
    """构造 ``DATA_BLOB``；同时返回底层缓冲区以保持引用有效。"""
    if not data:
        return _DataBlob(0, ctypes.POINTER(ctypes.c_char)()), None
    buffer = (ctypes.c_char * len(data)).from_buffer_copy(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _dpapi_protect(data: bytes) -> bytes:
    """用 DPAPI 加密（绑定当前 Windows 用户）。失败抛 ``OSError``。"""
    lib = _load_dpapi()
    if lib is None:
        raise OSError("DPAPI 不可用")
    in_blob, _in_buf = _make_blob(data)
    entropy_blob, _entropy_buf = _make_blob(STORE_SALT)
    out = _DataBlob()
    ok = lib["crypt32"].CryptProtectData(
        ctypes.byref(in_blob),
        _DPAPI_DESCRIPTION,
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise OSError(f"CryptProtectData 失败（Win32 错误码 {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(out.pbData, out.cbData) if out.pbData and out.cbData else b""
    finally:
        if out.pbData:
            lib["kernel32"].LocalFree(out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    """DPAPI 解密。失败抛 ``OSError``。"""
    lib = _load_dpapi()
    if lib is None:
        raise OSError("DPAPI 不可用")
    in_blob, _in_buf = _make_blob(data)
    entropy_blob, _entropy_buf = _make_blob(STORE_SALT)
    out = _DataBlob()
    ok = lib["crypt32"].CryptUnprotectData(
        ctypes.byref(in_blob),
        None,  # 不索取描述文本
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise OSError(f"CryptUnprotectData 失败（Win32 错误码 {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(out.pbData, out.cbData) if out.pbData and out.cbData else b""
    finally:
        if out.pbData:
            lib["kernel32"].LocalFree(out.pbData)


# ================================================================== 信封/校验
def _integrity_key() -> bytes:
    """HMAC 密钥 = SHA-256(盐 + 机器标识)，机器标识变化即视为异地文件。"""
    try:
        from .security import machine_id  # 延迟导入：避免模块级循环依赖

        machine = machine_id()
    except Exception:  # noqa: BLE001 - 机器标识读取失败不应导致存储崩溃
        log.warning("读取机器标识失败，使用固定回退值", exc_info=True)
        machine = "unknown-machine"
    return hashlib.sha256(STORE_SALT + b"|" + machine.encode("utf-8")).digest()


def _payload_mac(mode: str, payload: bytes) -> str:
    """计算 mode+payload 的 HMAC 十六进制串。"""
    message = mode.encode("ascii") + b"\x00" + payload
    return hmac.new(_integrity_key(), message, hashlib.sha256).hexdigest()


def _build_envelope(mode: str, payload: bytes) -> bytes:
    """把 payload 封装为可持久化的 JSON 信封。"""
    doc = {
        "magic": MAGIC,
        "version": FORMAT_VERSION,
        "mode": mode,
        "insecure": mode == "plain",
        "mac": _payload_mac(mode, payload),
        "data": base64.b64encode(payload).decode("ascii"),
    }
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _parse_envelope(raw: bytes) -> tuple[str, str, bytes] | None:
    """解析信封；结构不合法返回 None。"""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("magic") != MAGIC or doc.get("version") != FORMAT_VERSION:
        return None
    mode = doc.get("mode")
    mac_value = doc.get("mac")
    data = doc.get("data")
    if mode not in ("dpapi", "plain"):
        return None
    if not isinstance(mac_value, str) or not isinstance(data, str):
        return None
    try:
        payload = base64.b64decode(data.encode("ascii"), validate=True)
    except (ValueError, binascii.Error):
        return None
    return mode, mac_value, payload


# ================================================================== 账号存储
class AccountStore:
    """账号文件读写。所有失败都被吞掉并返回 ``None``（契约要求不得崩溃）。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else appdata_dir() / ACCOUNT_FILE
        #: 最近一次读/写是否为「明文回退」模式（仅信息用途，供 UI/日志提示）
        self.insecure = False

    # ------------------------------------------------------------ 公共 API
    def load(self) -> Account | None:
        """读取账号；文件不存在/损坏/被篡改/无法解密时返回 None。"""
        path = self.path
        if not path.is_file():
            return None
        try:
            raw = path.read_bytes()
        except OSError as exc:
            log.warning("读取账号文件失败 %s: %s", path, exc)
            return None

        parsed = _parse_envelope(raw)
        if parsed is None:
            self._discard("文件格式无效")
            return None
        mode, mac_value, payload = parsed
        expected = _payload_mac(mode, payload)
        if not hmac.compare_digest(expected, mac_value):
            self._discard("完整性校验失败（文件被篡改或来自其他电脑）")
            return None

        if mode == "dpapi":
            if not dpapi_available():
                log.warning("账号文件使用 DPAPI 加密，但当前系统不支持 DPAPI（非 Windows？）")
                return None
            try:
                plain = _dpapi_unprotect(payload)
            except OSError as exc:
                self._discard(f"DPAPI 解密失败：{exc}")
                return None
            self.insecure = False
        else:
            plain = payload
            self.insecure = True
            log.warning("账号文件为明文回退格式（%s），安全性较低", path)

        try:
            data = json.loads(plain.decode("utf-8"))
            account = Account.from_dict(data)
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            self._discard(f"账号数据无法解析：{exc}")
            return None
        return account

    def save(self, account: Account) -> None:
        """保存账号；Windows 上使用 DPAPI 加密。"""
        if not isinstance(account, Account):
            raise TypeError(f"save() 需要 Account，收到 {type(account).__name__}")
        raw = json.dumps(
            account.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if dpapi_available():
            payload = _dpapi_protect(raw)
            mode = "dpapi"
            self.insecure = False
        else:
            payload = raw
            mode = "plain"
            self.insecure = True
            log.warning("DPAPI 不可用，账号将以明文（含 HMAC 完整性校验）保存：%s", self.path)
        atomic_write_bytes(self.path, _build_envelope(mode, payload))
        if mode == "plain" and sys.platform != "win32":
            try:  # 尽力收紧权限（非 Windows 回退时）
                self.path.chmod(0o600)
            except OSError:
                pass

    def clear(self) -> None:
        """删除本地账号文件。"""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("删除账号文件失败 %s: %s", self.path, exc)
        self.insecure = False

    def days_since_verified(self, account: Account) -> float:
        """距离最近一次完整认证验证的天数；未知/非法时间返回 ``inf``。"""
        verified_at = float(getattr(account, "verified_at", 0.0) or 0.0)
        if verified_at <= 0.0:
            return float("inf")
        return max(0.0, (now() - verified_at) / 86400.0)

    def needs_reverify(self, account: Account, days: int = 30) -> bool:
        """是否已超过 ``days`` 天需要强制重新设备码登录（契约 §9）。"""
        try:
            limit_days = max(1, int(days))
        except (TypeError, ValueError):
            limit_days = 30
        verified_at = float(getattr(account, "verified_at", 0.0) or 0.0)
        if verified_at <= 0.0:
            return True
        return (now() - verified_at) > limit_days * 86400.0

    # ------------------------------------------------------------ 内部工具
    def _discard(self, reason: str) -> None:
        """清掉无效账号文件（契约要求：损坏文件不得反复报错）。"""
        log.warning("账号文件无效（%s），已清除：%s", reason, self.path)
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - 文件被占用等
            log.warning("清除无效账号文件失败 %s: %s", self.path, exc)
