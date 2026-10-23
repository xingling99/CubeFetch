"""本地防破解门禁：机器指纹、安装许可票据（Ticket）、AuthGate、完整性自检。

契约见 ``docs/CONTRACTS.md`` §9 security。设计说明：

- ``machine_id()``：优先注册表 ``HKLM\\SOFTWARE\\Microsoft\\Cryptography`` 的
  ``MachineGuid``，其次系统卷序列号，最后 ``uuid.getnode()`` 的哈希；返回哈希后的
  稳定标识（不暴露原始 GUID，且本模块从不外发）。
- ``Ticket.mac = HMAC-SHA256(key=SHA256(盐 + machine_id), msg="uuid|name|expires_at")``：
  票据无法伪造、无法跨机复用，过期即失效。
- ``AuthGate`` 是下载/安装的唯一入口守卫：``issue()`` 签发并激活票据，
  ``require_active()`` 供 install 层在无有效票据时抛 ``AuthError``；
  模块级 ``require_ticket()`` 与 ``clear_active()`` 供非 gate 持有方调用。
- 模块级 ``set_integrity_problems()`` / ``integrity_problems()`` 登记 app 启动时
  ``cubefetch.core.integrity.verify()`` 的结果：存在问题时 ``require_ticket()`` /
  ``require_active()`` 一律抛 ``AuthError(code="integrity")``，禁止下载/安装。
- ``IntegrityGuard.check()`` 务实自检（关键模块缺失、打包资源异常、调试器附加），
  返回中文异常列表，空列表表示通过。它只提高破解成本，不承诺绝对防破解。
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import importlib.util
import logging
import os
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auth import Account
from .errors import AuthError
from .util import now

log = logging.getLogger("cubefetch.security")

#: 域分离盐（machine_id / 票据各自独立，互不通用）
MACHINE_SALT = b"CubeFetch/machine-id/v1|a41d2c6f0b7e9358"
TICKET_SALT = b"CubeFetch/ticket/v1|5e8b3a1d9c7f2046"
#: 票据默认有效期（秒）。安装入口在每次安装前会重新 ensure_session 并重签票据。
DEFAULT_TICKET_TTL = 12 * 3600.0


# ================================================================== 机器标识
_machine_lock = threading.RLock()
_machine_id_cache: str | None = None


def _registry_machine_guid() -> str | None:
    """读取 HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # noqa: PLC0415 - 仅 Windows 存在
    except ImportError:
        return None
    try:
        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, access
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
    except OSError:
        return None
    text = str(value or "").strip().strip("{}").strip()
    return text or None


def _volume_serial() -> str | None:
    """读取系统卷序列号（GetVolumeInformationW）。"""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes  # noqa: PLC0415

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        serial = wintypes.DWORD(0)
        root = os.environ.get("SystemDrive", "C:") + "\\"
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            None,
            0,
            ctypes.byref(serial),
            None,
            None,
            None,
            0,
        )
        if not ok:
            return None
        return f"{serial.value:08X}"
    except (OSError, AttributeError):
        return None


def _node_hash() -> str:
    """最后兜底：基于网卡/节点号的哈希（不联网，仅本机使用）。"""
    node = uuid.getnode()
    return hashlib.sha256(b"CubeFetch/node/v1|" + str(node).encode("ascii")).hexdigest()


def _compute_machine_id() -> str:
    guid = _registry_machine_guid()
    if guid:
        return hashlib.sha256(MACHINE_SALT + b"|guid|" + guid.encode("utf-8")).hexdigest()
    serial = _volume_serial()
    if serial:
        return hashlib.sha256(MACHINE_SALT + b"|volume|" + serial.encode("ascii")).hexdigest()
    return _node_hash()


def machine_id() -> str:
    """返回本机稳定标识（哈希后的十六进制串，进程内缓存）。"""
    global _machine_id_cache
    with _machine_lock:
        if not _machine_id_cache:
            _machine_id_cache = _compute_machine_id()
        return _machine_id_cache


# ================================================================== 票据
def _ticket_mac(uuid_value: str, name: str, expires_at: float) -> str:
    """票据 HMAC。密钥由 machine_id + 内置盐派生（契约 §9）。"""
    key = hashlib.sha256(TICKET_SALT + b"|" + machine_id().encode("utf-8")).digest()
    message = f"{uuid_value}|{name}|{expires_at:.6f}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Ticket:
    """安装许可票据。UI 无法手工构造出合法 ``mac``。"""

    uuid: str
    name: str
    expires_at: float
    mac: str

    @classmethod
    def create(cls, account: Account, ttl: float = DEFAULT_TICKET_TTL) -> "Ticket":
        """为已认证账号签发票据。"""
        expires_at = now() + float(ttl)
        uuid_value = str(account.uuid)
        name = str(account.name)
        return cls(
            uuid=uuid_value,
            name=name,
            expires_at=expires_at,
            mac=_ticket_mac(uuid_value, name, expires_at),
        )

    def is_expired(self) -> bool:
        return now() >= self.expires_at

    def is_valid(self) -> bool:
        """mac 正确且未过期。"""
        if self.is_expired():
            return False
        if not self.uuid or not self.name or not self.mac:
            return False
        return hmac.compare_digest(self.mac, _ticket_mac(self.uuid, self.name, self.expires_at))


# ============================================================ 模块级激活状态
_active_lock = threading.RLock()
_active_ticket: Ticket | None = None
_active_account: Account | None = None


def _activate(ticket: Ticket, account: Account) -> None:
    global _active_ticket, _active_account
    with _active_lock:
        _active_ticket = ticket
        _active_account = account


def _active_snapshot() -> tuple[Ticket | None, Account | None]:
    with _active_lock:
        return _active_ticket, _active_account


def clear_active() -> None:
    """清除当前激活票据（登出/账号失效时调用）。"""
    global _active_ticket, _active_account
    with _active_lock:
        _active_ticket = None
        _active_account = None


def current_ticket() -> Ticket | None:
    """返回当前激活票据（无则 None）。"""
    with _active_lock:
        return _active_ticket


# ==================================================== 完整性校验结果门禁
_integrity_lock = threading.RLock()
_integrity_problems: list[str] = []


def set_integrity_problems(problems: list[str] | None) -> None:
    """登记程序文件完整性校验发现的问题（``None``/空列表 = 通过）。

    由 app 启动时的 ``cubefetch.core.integrity.verify()`` 结果驱动；登记后
    ``require_ticket()`` / ``require_active()`` 会一律拒绝下载/安装入口。
    """
    global _integrity_problems
    cleaned: list[str] = []
    for item in problems or []:
        text = str(item).strip()
        if text:
            cleaned.append(text)
    with _integrity_lock:
        _integrity_problems = cleaned
    if cleaned:
        log.warning(
            "完整性校验发现问题 %d 项，下载/安装已禁用：%s",
            len(cleaned),
            "；".join(cleaned[:5]),
        )


def integrity_problems() -> list[str]:
    """返回当前登记的文件完整性问题（副本；空列表表示校验通过）。"""
    with _integrity_lock:
        return list(_integrity_problems)


def _ensure_integrity_ok() -> None:
    """完整性校验失败时抛统一 ``AuthError``（``code="integrity"``）。"""
    problems = integrity_problems()
    if problems:
        first = problems[0] if problems else ""
        raise AuthError(
            "程序文件完整性校验失败，已禁止下载",
            detail=f"integrity_problems={len(problems)};first={first}",
            code="integrity",
        )


def require_ticket(ticket: Ticket | None = None) -> Account:
    """Installer 入口守卫（契约 §9）。

    - ``ticket`` 为 None：校验当前激活票据；无票据/票据失效抛 ``AuthError``。
    - 传入票据：校验其 mac 与有效期；与激活票据一致时返回完整 ``Account``，
      否则返回仅含身份信息的最小 ``Account``（安装流程只需身份标识）。
    - 完整性校验失败时（``set_integrity_problems`` 登记过问题）优先抛
      ``AuthError(code="integrity")``，无论是否已登录。
    """
    _ensure_integrity_ok()
    active_ticket, active_account = _active_snapshot()
    if ticket is None:
        if active_ticket is None:
            raise AuthError(
                "尚未登录 Minecraft 账号，请先使用 Microsoft 账号登录后再安装",
                detail="no_active_ticket",
                code="no_ticket",
            )
        ticket = active_ticket
        account: Account | None = active_account
    else:
        if not isinstance(ticket, Ticket):
            raise AuthError(
                "下载许可票据无效，请重新登录 Microsoft 账号",
                detail=f"bad_ticket_type:{type(ticket).__name__}",
                code="bad_ticket",
            )
        account = active_account if (active_ticket is not None and active_ticket == ticket) else None
    if not ticket.is_valid():
        if ticket is active_ticket:
            clear_active()
        raise AuthError(
            "登录许可已过期或被篡改，请重新登录 Microsoft 账号",
            detail=f"ticket_invalid:uuid={ticket.uuid},expires_at={ticket.expires_at}",
            code="bad_ticket",
        )
    if account is not None:
        return account
    return Account(
        access_token="",
        refresh_token="",
        expires_at=ticket.expires_at,
        uuid=ticket.uuid,
        name=ticket.name,
        verified_at=ticket.expires_at,
    )


def require_active() -> Account:
    """模块级便捷入口：等价于 ``require_ticket()``。"""
    return require_ticket()


# ================================================================== AuthGate
class AuthGate:
    """下载/安装的唯一入口守卫。只有 AuthService（经由本类）能签发 Ticket。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ticket: Ticket | None = None
        self._account: Account | None = None

    # ------------------------------------------------------------ 签发/校验
    def issue(self, account: Account) -> Ticket:
        """为已登录账号签发并激活票据。"""
        if not isinstance(account, Account):
            raise AuthError(
                "账号数据无效，请重新登录",
                detail=f"bad_account_type:{type(account).__name__}",
                code="bad_account",
            )
        if not account.uuid or not account.name:
            raise AuthError(
                "账号信息不完整，请重新登录", detail="missing_uuid_or_name", code="bad_account"
            )
        ticket = Ticket.create(account)
        with self._lock:
            self._ticket = ticket
            self._account = account
        _activate(ticket, account)
        log.debug("已签发安装许可票据 uuid=%s name=%s", account.uuid, account.name)
        return ticket

    def verify(self, ticket: Ticket) -> Account:
        """校验票据并返回对应账号；失败抛 ``AuthError``（契约 §9）。"""
        return require_ticket(ticket)

    def require_active(self) -> Account:
        """供 install 层调用：无有效票据时抛 ``AuthError``。"""
        return require_ticket()

    def require_ticket(self, ticket: Ticket | None = None) -> Account:
        """等价于模块级 ``require_ticket``，便于通过 gate 实例调用。"""
        return require_ticket(ticket)

    # ------------------------------------------------------------ 状态
    def current_ticket(self) -> Ticket | None:
        with self._lock:
            return self._ticket

    def active_account(self) -> Account | None:
        with self._lock:
            return self._account

    def clear(self) -> None:
        """登出：清除 gate 与模块级激活状态。"""
        with self._lock:
            self._ticket = None
            self._account = None
        clear_active()

    def clear_active(self) -> None:
        """``clear()`` 的别名（Lead 约定登出调用名）。"""
        self.clear()


# ============================================================= 完整性自检
class IntegrityGuard:
    """务实自检：发现明显异常时返回中文描述列表，空列表 = 通过。

    只做「提高破解成本」级别的检查：关键模块缺失、打包资源异常、调试器附加。
    不做签名验证/哈希白名单（无密钥基础设施，也避免拖慢启动）。
    """

    CRITICAL_MODULES = (
        "cubefetch.config",
        "cubefetch.core.errors",
        "cubefetch.core.util",
        "cubefetch.core.auth",
        "cubefetch.core.account_store",
        "cubefetch.core.security",
    )

    def check(self) -> list[str]:
        problems: list[str] = []
        for check in (self._check_modules, self._check_frozen, self._check_debugger):
            try:
                problems.extend(check())
            except Exception as exc:  # noqa: BLE001 - 自检本身不得崩溃
                problems.append(f"完整性自检异常（{check.__name__}）：{exc}")
        return problems

    # ------------------------------------------------------------ 子检查
    def _check_modules(self) -> list[str]:
        missing: list[str] = []
        for name in self.CRITICAL_MODULES:
            try:
                spec = importlib.util.find_spec(name)
            except (ImportError, ValueError):
                spec = None
            if spec is None:
                missing.append(name)
        if missing:
            return [f"关键模块缺失：{', '.join(missing)}"]
        return []

    def _check_frozen(self) -> list[str]:
        if not getattr(sys, "frozen", False):
            return []
        problems: list[str] = []
        exe = Path(sys.executable)
        if not exe.is_file():
            problems.append(f"程序主文件缺失：{exe}")
        meipass = getattr(sys, "_MEIPASS", None)
        if not meipass:
            problems.append("打包运行时缺少 _MEIPASS 资源目录（可能被二次打包/解包篡改）")
        else:
            bundle = Path(str(meipass))
            if not bundle.is_dir():
                problems.append(f"打包资源目录缺失：{bundle}")
        try:
            import cubefetch  # noqa: PLC0415 - 运行时包自身

            package_file = getattr(cubefetch, "__file__", "") or ""
            if not package_file:
                problems.append("无法定位 cubefetch 包（可能被裁剪）")
            elif meipass:
                package_dir = Path(package_file).resolve().parent
                bundle_root = Path(str(meipass)).resolve()
                if bundle_root not in package_dir.parents and package_dir != bundle_root:
                    problems.append(f"核心代码未从打包目录加载：{package_dir}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"无法导入 cubefetch 包：{exc}")
        return problems

    def _check_debugger(self) -> list[str]:
        try:
            if sys.gettrace() is not None:
                return ["检测到调试器/跟踪器已附加到进程"]
        except Exception:  # noqa: BLE001 - 某些嵌入式环境不支持 gettrace
            pass
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            try:
                if ctypes.windll.kernel32.IsDebuggerPresent():  # type: ignore[attr-defined]
                    return ["检测到调试器已附加到进程"]
            except (OSError, AttributeError):
                pass
        return []


__all__ = [
    "AuthGate",
    "DEFAULT_TICKET_TTL",
    "IntegrityGuard",
    "Ticket",
    "clear_active",
    "current_ticket",
    "integrity_problems",
    "machine_id",
    "require_active",
    "require_ticket",
    "set_integrity_problems",
]
