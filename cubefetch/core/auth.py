"""Microsoft → Xbox Live → Minecraft（Java 版）认证。

实现设备码登录、静默刷新与身份复核。所有网络/协议错误都会转换为中文
:class:`cubefetch.core.errors.AuthError`（或 ``NotOwnedError`` / ``CancelledError``），
不会把 requests 的异常裸抛给上层。

端点与错误码依据 ``docs/research/auth.md``（2026-10-20 调研）以及 Lead 的实测结论：

- 设备码 / 轮询 / 刷新：``login.live.com`` 旧 Live Connect 端点（client_id
  ``00000000402b5328``）。微软新版 ``consumers/oauth2/v2.0/devicecode`` 对该 client_id
  返回 AADSTS700016，不可用。
- XBL → XSTS → Minecraft：固定三跳 JSON 交换，见下方 URL 常量。
- 所有权由 ``entitlements/mcstore`` 的 item 名称决定，档案由 ``minecraft/profile`` 决定。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import AuthError, CancelledError, NotOwnedError
from .util import now

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查，运行时不依赖 http.py 已就绪
    from .account_store import AccountStore
    from .http import HttpSession

log = logging.getLogger("cubefetch.auth")

# ---------------------------------------------------------------- 端点与常量
MSA_CLIENT_ID = "00000000402b5328"
MSA_SCOPE = "XboxLive.signin offline_access"

DEVICE_CODE_URL = "https://login.live.com/oauth20_connect.srf"
TOKEN_URL = "https://login.live.com/oauth20_token.srf"
XBL_USER_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
XSTS_RELYING_PARTY = "rp://api.minecraftservices.com/"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_ENTITLEMENTS_URL = "https://api.minecraftservices.com/entitlements/mcstore"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

DEFAULT_VERIFICATION_URI = "https://www.microsoft.com/link"
DEFAULT_TIMEOUT = 30.0

#: status_cb 使用的中文阶段文案
MSG_WAITING = "正在等待浏览器授权…"
MSG_XBOX = "正在验证 Xbox 账号…"
MSG_XSTS = "正在验证 Xbox 服务（XSTS）…"
MSG_MC_LOGIN = "正在登录 Minecraft 服务…"
MSG_ENTITLEMENTS = "正在验证游戏所有权…"
MSG_PROFILE = "正在获取游戏档案…"

_JSON_HEADERS = {"Accept": "application/json"}
_XBL_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "x-xbl-contract-version": "1",
}
_NETWORK_ERROR_MESSAGE = "无法连接 Microsoft 登录服务器，请检查网络连接或代理设置后重试"

#: 设备码轮询时非致命、需要继续等待的 error 值
_PENDING_ERRORS = frozenset({"authorization_pending"})

#: XSTS ``XErr`` 错误码 → (中文提示, 稳定错误码)。依据 docs/research/auth.md §10.1。
XERR_MESSAGES: dict[int, tuple[str, str]] = {
    2148916227: ("该账号因违反 Xbox 社区规范已被封禁，无法登录", "xbox_banned"),
    2148916229: (
        "该账号受家长控制限制，监护人尚未授予在线权限（请访问 "
        "https://account.microsoft.com/family/ 处理）",
        "guardian_restricted",
    ),
    2148916233: (
        "该 Microsoft 账号尚未创建 Xbox 档案，请先访问 "
        "https://signup.live.com/signup 创建后再试",
        "no_xbox_account",
    ),
    2148916234: (
        "该账号尚未同意 Xbox 服务条款，请先在 https://www.xbox.com 登录并同意条款",
        "xbox_tos_required",
    ),
    2148916235: ("该账号所在国家/地区不支持 Xbox Live 登录", "region_unsupported"),
    2148916236: (
        "该账号需要完成年龄验证，请打开 https://login.live.com/login.srf 完成验证后重试",
        "age_verification_required",
    ),
    2148916237: (
        "该账号已达到家庭组设置的游戏时长限制，请让家长调整限制后重试",
        "playtime_limit",
    ),
    2148916238: (
        "该账号未满 18 周岁且未加入 Microsoft 家庭组，请让家长将账号加入家庭组后重试",
        "child_account",
    ),
}


class _NoopStatus:
    """status_cb 缺省占位。"""

    def __call__(self, _message: str) -> None:  # pragma: no cover - 显然
        return None


_NO_STATUS: Callable[[str], None] = _NoopStatus()


# ---------------------------------------------------------------- 数据结构
@dataclass
class DeviceFlow:
    """设备码登录流程中需要展示给用户的凭据。"""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass
class Account:
    """已登录的 Minecraft 账号（access_token 为 Minecraft services 的 token）。"""

    access_token: str
    refresh_token: str
    expires_at: float
    uuid: str
    name: str
    xuid: str | None = None
    verified_at: float = 0.0  # 完整认证链最近一次验证成功的时间戳

    def token_valid(self) -> bool:
        """Minecraft 访问令牌是否仍未过期。"""
        return bool(self.access_token) and now() < float(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可用的字典（供 account_store 使用）。"""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": float(self.expires_at),
            "uuid": self.uuid,
            "name": self.name,
            "xuid": self.xuid,
            "verified_at": float(self.verified_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        """从字典恢复账号；数据不完整时抛 ``ValueError``。"""
        if not isinstance(data, dict):
            raise ValueError("账号数据格式不正确")
        access_token = data.get("access_token")
        uuid_value = data.get("uuid")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("账号数据缺少 access_token")
        if not isinstance(uuid_value, str) or not uuid_value:
            raise ValueError("账号数据缺少 uuid")
        xuid = data.get("xuid")
        return cls(
            access_token=access_token,
            refresh_token=str(data.get("refresh_token") or ""),
            expires_at=_as_float(data.get("expires_at"), 0.0),
            uuid=uuid_value,
            name=str(data.get("name") or ""),
            xuid=str(xuid) if xuid not in (None, "") else None,
            verified_at=_as_float(data.get("verified_at"), 0.0),
        )


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 内部工具
def _status_of(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _json_of(response: Any) -> dict[str, Any] | None:
    """尽力解析响应体；不是 JSON / 非对象时返回 None。"""
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - 响应体不是 JSON 属于可预期情况
        return None
    return data if isinstance(data, dict) else None


def _text_of(response: Any, limit: int = 400) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(text, str):
        text = str(text)
    return text[:limit]


def _oauth_error_message(status: int, error: str, description: str) -> str:
    """把 OAuth 端点错误映射成中文提示（不含轮询中的 pending/slow_down）。"""
    if status == 429:
        return "请求过于频繁，请稍等片刻后重试"
    if status >= 500:
        return f"Microsoft 登录服务器暂时不可用（HTTP {status}），请稍后重试"
    if error == "invalid_client":
        return "登录服务配置错误（invalid_client），请联系开发者"
    if error == "invalid_request":
        return "登录请求无效（invalid_request），请稍后重试"
    if error == "invalid_scope":
        return "登录权限范围无效（invalid_scope），请联系开发者"
    if error in ("invalid_grant", "expired_token", "code_expired"):
        return "登录凭据已失效或过期，请重新开始登录"
    if error:
        return f"登录失败（{error}），请稍后重试"
    return f"登录服务器返回错误（HTTP {status}），请稍后重试"


def _parse_xerr(response: Any, body: dict[str, Any] | None) -> int | None:
    """XErr 可能出现在 ``x-err`` 响应头（prismarine-auth）或 JSON 体（Prism/HMCL）。"""
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw = headers.get("x-err") or headers.get("X-Err")
        except (AttributeError, TypeError):
            raw = None
        if raw not in (None, ""):
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError):
                pass
    if isinstance(body, dict) and body.get("XErr") is not None:
        try:
            return int(str(body["XErr"]).strip())
        except (TypeError, ValueError):
            return None
    return None


def _xerr_error(xerr: int) -> AuthError:
    message, code = XERR_MESSAGES.get(
        xerr, (f"Xbox Live 登录失败（XErr {xerr}），请稍后重试", f"xerr_{xerr}")
    )
    return AuthError(message, detail=f"XErr={xerr}", code=code)


# ---------------------------------------------------------------- 认证服务
class AuthService:
    """设备码登录 / 刷新 / 身份复核。

    ``http`` 参数在契约中是 :class:`cubefetch.core.http.HttpSession`。契约 §3 的
    HttpSession 只提供了 GET 下载接口，而认证需要带 header 的 POST 与 Bearer GET，
    因此这里做兼容处理：

    1. 传入对象有可调用的 ``.request`` 方法 → 直接使用；
    2. 传入对象有 ``.session`` / ``._session``（requests.Session）→ 使用其 ``request``；
    3. 否则内部自建 ``requests.Session``（认证请求不经过下载镜像）。
    """

    def __init__(
        self,
        http: "HttpSession | Any",
        store: "AccountStore | Any",
        remember_days: int = 30,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._http = http
        self.store = store
        try:
            self.remember_days = max(1, int(remember_days))
        except (TypeError, ValueError):
            self.remember_days = 30
        self.timeout = float(timeout)
        self._own_session: Any = None
        self._transport_lock = threading.Lock()

    # -------------------------------------------------------- 传输层
    def _transport(self) -> Callable[..., Any]:
        """返回一个 ``request(method, url, **kwargs)`` 可调用对象。"""
        target = self._http
        if target is not None:
            request = getattr(target, "request", None)
            if callable(request):
                return request
            for attr in ("session", "_session"):
                session = getattr(target, attr, None)
                if session is not None and callable(getattr(session, "request", None)):
                    return session.request
        with self._transport_lock:
            if self._own_session is None:
                try:
                    import requests  # noqa: PLC0415 - 仅在真正需要时加载
                except ImportError as exc:  # pragma: no cover - requests 是硬依赖
                    raise AuthError(
                        "网络组件缺失，请重新安装 CubeFetch",
                        detail=f"import requests failed: {exc}",
                        code="internal",
                    ) from exc
                self._own_session = requests.Session()
            return self._own_session.request

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """发出请求，并把所有传输层异常翻译成中文 AuthError。"""
        transport = self._transport()
        try:
            return transport(
                method,
                url,
                params=params,
                data=data,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
            )
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转换为业务异常
            raise AuthError(
                _NETWORK_ERROR_MESSAGE,
                detail=f"{type(exc).__name__}: {exc}",
                code="network",
            ) from exc

    # -------------------------------------------------------- 1. 设备码登录
    def start_login(self) -> DeviceFlow:
        """请求设备码；用户需要在浏览器中打开 ``verification_uri`` 并输入 ``user_code``。"""
        response = self._request(
            "POST",
            DEVICE_CODE_URL,
            data={
                "client_id": MSA_CLIENT_ID,
                "scope": MSA_SCOPE,
                "response_type": "device_code",
            },
            headers=_JSON_HEADERS,
        )
        status = _status_of(response)
        body = _json_of(response)
        if status != 200 or not body:
            error = str((body or {}).get("error") or "")
            description = str((body or {}).get("error_description") or "")
            raise AuthError(
                _oauth_error_message(status, error, description),
                detail=f"device code: HTTP {status} error={error!r} {description} {_text_of(response)}",
                code=error or ("http" if status else "network"),
            )
        device_code = body.get("device_code")
        user_code = body.get("user_code")
        if not device_code or not user_code:
            raise AuthError(
                "获取设备登录代码失败：登录服务器返回的数据不完整，请稍后重试",
                detail=f"body={str(body)[:300]}",
                code="protocol",
            )
        expires_in = int(_as_float(body.get("expires_in"), 900.0))
        interval = int(_as_float(body.get("interval"), 5.0))
        return DeviceFlow(
            device_code=str(device_code),
            user_code=str(user_code),
            verification_uri=str(body.get("verification_uri") or DEFAULT_VERIFICATION_URI),
            expires_in=max(1, expires_in),
            # interval 允许为 0：仅用于测试注入的假响应，真实服务返回 5
            interval=max(0, interval),
        )

    def wait_login(
        self,
        flow: DeviceFlow,
        cancel: threading.Event,
        status_cb: Callable[[str], None] | None = None,
    ) -> Account:
        """阻塞等待用户完成浏览器授权，并跑完整个 Xbox → Minecraft 认证链。

        轮询间隔遵守 ``flow.interval``（``slow_down`` 时 +5 秒），``cancel.set()`` 后
        最多等待一个间隔就会抛出 :class:`CancelledError`。
        """
        if not isinstance(flow, DeviceFlow):
            raise AuthError("登录流程参数无效，请重新开始登录", code="bad_flow")
        callback = status_cb or _NO_STATUS
        event = cancel if cancel is not None else threading.Event()

        msa = self._poll_device_token(flow, event, callback)
        account = self._authenticate_msa(msa, callback)
        self._save(account)
        return account

    def _poll_device_token(
        self,
        flow: DeviceFlow,
        cancel: threading.Event,
        status_cb: Callable[[str], None],
    ) -> dict[str, Any]:
        interval = max(0.0, float(flow.interval))
        deadline = now() + max(1.0, float(flow.expires_in))
        form = {
            "client_id": MSA_CLIENT_ID,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": flow.device_code,
        }
        while True:
            if cancel.is_set():
                raise CancelledError("登录已取消")
            if now() >= deadline:
                raise AuthError("设备登录代码已过期，请重新开始登录", code="expired_token")
            response = self._request("POST", TOKEN_URL, data=form, headers=_JSON_HEADERS)
            status = _status_of(response)
            body = _json_of(response) or {}
            if status == 200 and body.get("access_token"):
                return body
            error = str(body.get("error") or "")
            description = str(body.get("error_description") or "")

            if error in _PENDING_ERRORS:
                status_cb(MSG_WAITING)
                if cancel.wait(interval):
                    raise CancelledError("登录已取消")
                continue
            if error == "slow_down":
                # RFC 8628：slow_down 后把轮询间隔增加 5 秒
                interval += 5.0
                status_cb(MSG_WAITING)
                if cancel.wait(interval):
                    raise CancelledError("登录已取消")
                continue
            if error in ("expired_token", "code_expired"):
                raise AuthError("设备登录代码已过期，请重新开始登录", detail=description, code="expired_token")
            if error == "invalid_grant":
                if "must include" in description:
                    raise AuthError(
                        "登录服务返回参数错误（invalid_grant），请稍后重试",
                        detail=description,
                        code="protocol",
                    )
                raise AuthError(
                    "设备登录代码无效或已过期，请重新开始登录",
                    detail=description,
                    code="device_code",
                )
            if error:
                raise AuthError(
                    _oauth_error_message(status, error, description),
                    detail=f"token poll: HTTP {status} {description}",
                    code=error,
                )
            raise AuthError(
                _oauth_error_message(status, "", ""),
                detail=f"token poll: HTTP {status} {_text_of(response)}",
                code="http" if status else "network",
            )

    # -------------------------------------------------------- 2. 刷新
    def refresh(self, account: Account) -> Account:
        """静默刷新账号：仅用于未超过 ``remember_days`` 的账号。

        超过期限（或从未完成完整验证）的账号禁止用 refresh token 恢复，必须重新设备码登录。
        """
        if not isinstance(account, Account):
            raise AuthError("账号数据无效，请重新登录", code="bad_account")
        if self._needs_reverify(account):
            raise AuthError(
                f"账号验证已超过 {self.remember_days} 天，为保护账号安全，请重新使用设备码登录",
                detail=f"verified_at={account.verified_at}",
                code="reverify",
            )
        if not account.refresh_token:
            raise AuthError("登录状态已失效，请重新使用设备码登录", code="no_refresh_token")

        msa = self._refresh_msa(account.refresh_token)
        mc_token, refresh_token, expires_in = self._msa_to_mc(
            msa, refresh_fallback=account.refresh_token
        )
        refreshed = Account(
            access_token=mc_token,
            refresh_token=refresh_token,
            expires_at=now() + expires_in,
            uuid=account.uuid,
            name=account.name,
            xuid=account.xuid,
            # 静默刷新不算“完整链重新验证”，不能借此延长 30 天期限
            verified_at=float(account.verified_at or 0.0),
        )
        self._save(refreshed)
        return refreshed

    def verify_identity(self, account: Account) -> None:
        """重新拉取 entitlements + profile，校验账号仍有效并更新 ``verified_at``。

        这是用户主动/定期完成的完整复核，不受 30 天限制（到期后正是靠它之外的方式重登），
        但会重新验证所有权并刷新档案信息。
        """
        if not isinstance(account, Account):
            raise AuthError("账号数据无效，请重新登录", code="bad_account")
        current = account
        renewed = False
        if not current.token_valid():
            current = self._renew_account(current)
            renewed = True
        try:
            self._check_entitlements(current.access_token)
            profile = self._fetch_profile(current.access_token)
        except AuthError as exc:
            # token 被服务端提前吊销/时钟偏差：刷新一次后重试
            if exc.code != "unauthorized" or renewed:
                raise
            current = self._renew_account(current)
            self._check_entitlements(current.access_token)
            profile = self._fetch_profile(current.access_token)

        account.access_token = current.access_token
        account.refresh_token = current.refresh_token
        account.expires_at = current.expires_at
        account.uuid = str(profile.get("id") or account.uuid)
        account.name = str(profile.get("name") or account.name)
        account.verified_at = now()
        self._save(account)

    # -------------------------------------------------------- 3. MSA 层
    def _refresh_msa(self, refresh_token: str) -> dict[str, Any]:
        if not refresh_token:
            raise AuthError("登录状态已失效，请重新使用设备码登录", code="no_refresh_token")
        response = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": MSA_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": MSA_SCOPE,
            },
            headers=_JSON_HEADERS,
        )
        status = _status_of(response)
        body = _json_of(response) or {}
        if status != 200 or not body.get("access_token"):
            error = str(body.get("error") or "")
            description = str(body.get("error_description") or "")
            if error == "invalid_grant" or status in (400, 401):
                raise AuthError(
                    "登录状态已失效，请重新使用设备码登录",
                    detail=f"refresh: HTTP {status} error={error!r} {description}",
                    code="invalid_grant",
                )
            raise AuthError(
                _oauth_error_message(status, error, description),
                detail=f"refresh: HTTP {status} {_text_of(response)}",
                code=error or ("http" if status else "network"),
            )
        # 微软每次刷新都会轮换 refresh_token，务必持久化新的；极少数情况不返回则沿用旧的
        body["refresh_token"] = body.get("refresh_token") or refresh_token
        return body

    def _needs_reverify(self, account: Account) -> bool:
        store = self.store
        if store is not None:
            try:
                return bool(store.needs_reverify(account, days=self.remember_days))
            except TypeError:
                try:
                    return bool(store.needs_reverify(account))
                except Exception as exc:  # noqa: BLE001
                    log.warning("账号存储 needs_reverify 调用失败: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("账号存储 needs_reverify 调用失败: %s", exc)
        verified_at = float(getattr(account, "verified_at", 0.0) or 0.0)
        if verified_at <= 0.0:
            return True
        return (now() - verified_at) > self.remember_days * 86400.0

    # -------------------------------------------------------- 4. Xbox/MC 链
    def _authenticate_msa(
        self, msa: dict[str, Any], status_cb: Callable[[str], None]
    ) -> Account:
        """完整认证链：MSA token → XBL → XSTS → Minecraft → 所有权 → 档案。"""
        mc_token, refresh_token, expires_in = self._msa_to_mc(msa, status_cb=status_cb)
        status_cb(MSG_ENTITLEMENTS)
        self._check_entitlements(mc_token)
        status_cb(MSG_PROFILE)
        profile = self._fetch_profile(mc_token)
        return Account(
            access_token=mc_token,
            refresh_token=refresh_token,
            expires_at=now() + expires_in,
            uuid=str(profile["id"]),
            name=str(profile["name"]),
            xuid=None,
            verified_at=now(),
        )

    def _msa_to_mc(
        self,
        msa: dict[str, Any],
        *,
        status_cb: Callable[[str], None] | None = None,
        refresh_fallback: str = "",
    ) -> tuple[str, str, float]:
        """把 MSA access token 换成 Minecraft access token。"""
        callback = status_cb or _NO_STATUS
        msa_access_token = str(msa.get("access_token") or "")
        if not msa_access_token:
            raise AuthError(
                "登录服务器返回的数据不完整，请重新登录",
                detail=f"msa keys={sorted(msa)}",
                code="protocol",
            )
        callback(MSG_XBOX)
        xbl_token, xbl_uhs = self._xbox_user_token(msa_access_token)
        callback(MSG_XSTS)
        xsts_token, uhs = self._xsts_authorize(xbl_token, xbl_uhs)
        callback(MSG_MC_LOGIN)
        mc_token, expires_in = self._login_with_xbox(uhs, xsts_token)
        refresh_token = str(msa.get("refresh_token") or refresh_fallback or "")
        return mc_token, refresh_token, expires_in

    def _renew_account(self, account: Account) -> Account:
        """用 refresh token 重新铸造 Minecraft token（不做 30 天门禁，供 verify_identity）。"""
        if not account.refresh_token:
            raise AuthError("登录状态已失效，请重新使用设备码登录", code="no_refresh_token")
        msa = self._refresh_msa(account.refresh_token)
        mc_token, refresh_token, expires_in = self._msa_to_mc(
            msa, refresh_fallback=account.refresh_token
        )
        return Account(
            access_token=mc_token,
            refresh_token=refresh_token,
            expires_at=now() + expires_in,
            uuid=account.uuid,
            name=account.name,
            xuid=account.xuid,
            verified_at=float(account.verified_at or 0.0),
        )

    def _xbox_user_token(self, msa_access_token: str) -> tuple[str, str]:
        """POST user.auth.xboxlive.com → (UserToken, uhs)。"""
        body = {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"d={msa_access_token}",
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        }
        response = self._request("POST", XBL_USER_URL, json_body=body, headers=_XBL_HEADERS)
        status = _status_of(response)
        parsed = _json_of(response)
        if status != 200:
            xerr = _parse_xerr(response, parsed)
            if xerr is not None:
                raise _xerr_error(xerr)
            raise AuthError(
                f"Xbox 账号验证失败（HTTP {status}），请稍后重试",
                detail=f"xbl: {_text_of(response)}",
                code="xbl" if status else "network",
            )
        return _extract_xbox_token(parsed, "Xbox 账号验证")

    def _xsts_authorize(self, user_token: str, user_uhs: str) -> tuple[str, str]:
        """POST xsts.auth.xboxlive.com → (XSTS Token, uhs)，并校验 uhs 一致。"""
        body = {
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [user_token]},
            "RelyingParty": XSTS_RELYING_PARTY,
            "TokenType": "JWT",
        }
        response = self._request("POST", XSTS_URL, json_body=body, headers=_XBL_HEADERS)
        status = _status_of(response)
        parsed = _json_of(response)
        if status != 200:
            xerr = _parse_xerr(response, parsed)
            if xerr is not None:
                raise _xerr_error(xerr)
            raise AuthError(
                f"Xbox 服务授权失败（HTTP {status}），请稍后重试",
                detail=f"xsts: {_text_of(response)}",
                code="xsts" if status else "network",
            )
        if not parsed:
            raise AuthError(
                "Xbox 服务授权失败：登录服务器返回的数据不完整，请稍后重试",
                detail="xsts: empty body",
                code="protocol",
            )
        token = parsed.get("Token")
        if not token:
            raise AuthError(
                "Xbox 服务授权失败：登录服务器返回的数据不完整，请稍后重试",
                detail=f"xsts body={str(parsed)[:300]}",
                code="protocol",
            )
        xsts_uhs = _first_uhs(parsed) or user_uhs
        if user_uhs and xsts_uhs and xsts_uhs != user_uhs:
            raise AuthError(
                "Xbox 身份校验失败（用户哈希不一致），请重新登录",
                detail=f"xbl_uhs={user_uhs} xsts_uhs={xsts_uhs}",
                code="protocol",
            )
        return str(token), xsts_uhs

    def _login_with_xbox(self, uhs: str, xsts_token: str) -> tuple[str, float]:
        """POST authentication/login_with_xbox → (Minecraft access_token, expires_in 秒)。"""
        body = {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"}
        response = self._request("POST", MC_LOGIN_URL, json_body=body, headers=_JSON_HEADERS)
        status = _status_of(response)
        parsed = _json_of(response) or {}
        token = parsed.get("access_token")
        if status == 401:
            raise AuthError(
                "Minecraft 登录失败（身份令牌被拒绝），请重新登录",
                detail=f"login_with_xbox: {_text_of(response)}",
                code="unauthorized",
            )
        if status == 429:
            raise AuthError("请求过于频繁，请稍等片刻后重试", code="rate_limited")
        if status >= 500:
            raise AuthError(
                f"Minecraft 服务器暂时不可用（HTTP {status}），请稍后重试", code="http"
            )
        if status != 200 or not token:
            raise AuthError(
                f"Minecraft 登录失败（HTTP {status}），请稍后重试",
                detail=f"login_with_xbox: {_text_of(response)}",
                code="http",
            )
        return str(token), max(1.0, _as_float(parsed.get("expires_in"), 86400.0))

    # -------------------------------------------------------- 5. 所有权/档案
    def _check_entitlements(self, mc_token: str) -> bool:
        """检查账号是否拥有 Minecraft；没有则抛 :class:`NotOwnedError`。"""
        response = self._request(
            "GET",
            MC_ENTITLEMENTS_URL,
            headers={"Authorization": f"Bearer {mc_token}", "Accept": "application/json"},
        )
        status = _status_of(response)
        if status == 401:
            raise AuthError(
                "Minecraft 登录凭证已失效，请重新登录",
                detail=f"entitlements: {_text_of(response)}",
                code="unauthorized",
            )
        if status == 429:
            raise AuthError("请求过于频繁，请稍等片刻后重试", code="rate_limited")
        if status >= 500:
            raise AuthError(
                f"Minecraft 服务器暂时不可用（HTTP {status}），请稍后重试", code="http"
            )
        if status != 200:
            raise AuthError(
                f"验证游戏所有权失败（HTTP {status}），请稍后重试",
                detail=f"entitlements: {_text_of(response)}",
                code="http",
            )
        parsed = _json_of(response) or {}
        names: set[str] = set()
        items = parsed.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("name"):
                    names.add(str(item["name"]))
        owned = "product_minecraft" in names or "game_minecraft" in names
        if not owned:
            raise NotOwnedError(detail=f"entitlements items={sorted(names)}")
        return owned

    def _fetch_profile(self, mc_token: str) -> dict[str, str]:
        """GET minecraft/profile → {"id": 无横线 UUID, "name": 玩家名}。"""
        response = self._request(
            "GET",
            MC_PROFILE_URL,
            headers={"Authorization": f"Bearer {mc_token}", "Accept": "application/json"},
        )
        status = _status_of(response)
        if status == 401:
            raise AuthError(
                "Minecraft 登录凭证已失效，请重新登录",
                detail=f"profile: {_text_of(response)}",
                code="unauthorized",
            )
        if status == 404:
            raise AuthError(
                "该账号拥有 Minecraft，但尚未创建游戏档案；请先在官方启动器创建用户名后重试",
                detail="profile: HTTP 404",
                code="no_profile",
            )
        if status == 429:
            raise AuthError("请求过于频繁，请稍等片刻后重试", code="rate_limited")
        if status >= 500:
            raise AuthError(
                f"Minecraft 服务器暂时不可用（HTTP {status}），请稍后重试", code="http"
            )
        if status != 200:
            raise AuthError(
                f"获取游戏档案失败（HTTP {status}），请稍后重试",
                detail=f"profile: {_text_of(response)}",
                code="http",
            )
        parsed = _json_of(response)
        if not parsed:
            raise AuthError(
                "获取游戏档案失败：登录服务器返回的数据不完整，请稍后重试",
                detail="profile: empty body",
                code="protocol",
            )
        profile_id = parsed.get("id")
        name = parsed.get("name")
        if not profile_id or not name:
            raise AuthError(
                "获取游戏档案失败：登录服务器返回的数据不完整，请稍后重试",
                detail=f"profile body={str(parsed)[:300]}",
                code="protocol",
            )
        uuid_value = str(profile_id).replace("-", "").lower()
        if len(uuid_value) != 32:
            raise AuthError(
                "获取游戏档案失败：账号 UUID 格式不正确，请稍后重试",
                detail=f"profile id={profile_id!r}",
                code="protocol",
            )
        return {"id": uuid_value, "name": str(name)}

    # -------------------------------------------------------- 6. 持久化
    def _save(self, account: Account) -> None:
        store = self.store
        if store is None:
            return
        try:
            store.save(account)
        except Exception as exc:  # noqa: BLE001 - 保存失败不应让登录整体失败
            log.warning("保存账号失败: %s", exc)


def _first_uhs(data: dict[str, Any]) -> str | None:
    """从 ``DisplayClaims.xui`` 数组中取第一个 uhs。"""
    claims = data.get("DisplayClaims")
    if not isinstance(claims, dict):
        return None
    xui = claims.get("xui")
    if not isinstance(xui, list):
        return None
    for entry in xui:
        if isinstance(entry, dict) and entry.get("uhs"):
            return str(entry["uhs"])
    return None


def _extract_xbox_token(data: dict[str, Any] | None, what: str) -> tuple[str, str]:
    """从 XBL/XSTS 风格响应中取出 (Token, uhs)。"""
    if not isinstance(data, dict):
        raise AuthError(
            f"{what}失败：登录服务器返回的数据不完整，请稍后重试",
            detail="empty body",
            code="protocol",
        )
    token = data.get("Token")
    uhs = _first_uhs(data)
    if not token or not uhs:
        raise AuthError(
            f"{what}失败：登录服务器返回的数据不完整，请稍后重试",
            detail=f"missing Token/uhs: {str(data)[:300]}",
            code="protocol",
        )
    return str(token), uhs
