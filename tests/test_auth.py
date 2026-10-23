"""认证模块单测：全部使用假 HTTP 对象，绝不联网。

覆盖：设备码流程、轮询（pending/slow_down/过期/invalid_grant）、取消语义、
错误映射（OAuth / XSTS XErr / 网络异常）、完整认证链、30 天重验门禁、
refresh、verify_identity、状态文案与持久化调用。
"""
from __future__ import annotations

import json
import threading
import unittest
from dataclasses import dataclass

from cubefetch.core import auth as auth_module
from cubefetch.core.auth import (
    Account,
    AuthService,
    DeviceFlow,
    MC_ENTITLEMENTS_URL,
    MC_LOGIN_URL,
    MC_PROFILE_URL,
    MSG_ENTITLEMENTS,
    MSG_MC_LOGIN,
    MSG_PROFILE,
    MSG_WAITING,
    MSG_XBOX,
    MSG_XSTS,
    TOKEN_URL,
    DEVICE_CODE_URL,
    XBL_USER_URL,
    XSTS_URL,
)
from cubefetch.core.errors import AuthError, CancelledError, NotOwnedError
from cubefetch.core.util import now

# ------------------------------------------------------------------ 假对象
class FakeResponse:
    """最小 requests.Response 替身。"""

    def __init__(self, status_code: int = 200, body=None, headers=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text else (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class FakeHttp:
    """按顺序吐出预置响应/异常；记录每次调用参数。"""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"未预置响应的请求: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


class FakeStore:
    """AccountStore 替身。"""

    def __init__(self, reverify: bool = False, reverify_error: BaseException | None = None):
        self.reverify = reverify
        self.reverify_error = reverify_error
        self.reverify_calls: list[tuple[Account, int]] = []
        self.saved: list[Account] = []

    def needs_reverify(self, account, days: int = 30) -> bool:
        self.reverify_calls.append((account, days))
        if self.reverify_error is not None:
            raise self.reverify_error
        return self.reverify

    def save(self, account: Account) -> None:
        self.saved.append(account)


class FakeCancel(threading.Event):
    """可编程的取消事件：wait() 返回预置结果，并记录超时参数。"""

    def __init__(self, results=None):
        super().__init__()
        self.results = list(results or [])
        self.waits: list[float | None] = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.results:
            return self.results.pop(0)
        return False


# ------------------------------------------------------------------ 固定数据
DEVICE_BODY = {
    "device_code": "device-code-1",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://www.microsoft.com/link",
    "expires_in": 900,
    "interval": 0,
}
MSA_BODY = {"access_token": "msa-access", "refresh_token": "msa-refresh-1", "expires_in": 3600}
XBL_BODY = {"Token": "xbl-token-1", "DisplayClaims": {"xui": [{"uhs": "uhs-abc"}]}}
XSTS_BODY = {"Token": "xsts-token-1", "DisplayClaims": {"xui": [{"uhs": "uhs-abc"}]}}
MC_LOGIN_BODY = {"access_token": "mc-token-1", "expires_in": 86400}
ENTITLEMENTS_BODY = {"items": [{"name": "product_minecraft"}, {"name": "game_minecraft"}]}
PROFILE_BODY = {"id": "069A79F4-44E9-4726-A5BE-FCA90E38AFFA", "name": "Herobrine"}
EXPECTED_UUID = "069a79f444e94726a5befca90e38affa"


def _resp(status: int = 200, body=None, headers=None, text: str = "") -> FakeResponse:
    return FakeResponse(status, body, headers, text)


def _device_resp() -> FakeResponse:
    return _resp(body=dict(DEVICE_BODY))


def _chain_after_msa() -> list[FakeResponse]:
    """完整链在拿到 MSA token 之后的部分（XBL → XSTS → MC → 所有权 → 档案）。"""
    return [
        _resp(body=dict(XBL_BODY)),
        _resp(body=dict(XSTS_BODY)),
        _resp(body=dict(MC_LOGIN_BODY)),
        _resp(body=dict(ENTITLEMENTS_BODY)),
        _resp(body=dict(PROFILE_BODY)),
    ]


def _full_login_chain() -> list[FakeResponse]:
    return [_resp(body=dict(MSA_BODY))] + _chain_after_msa()


def _refresh_chain() -> list[FakeResponse]:
    return [_resp(body=dict(MSA_BODY))] + [
        _resp(body=dict(XBL_BODY)),
        _resp(body=dict(XSTS_BODY)),
        _resp(body=dict(MC_LOGIN_BODY)),
    ]


def _account(verified_days_ago: float = 0.0, expires_in: float = 3600.0) -> Account:
    return Account(
        access_token="mc-old",
        refresh_token="rt-old",
        expires_at=now() + expires_in,
        uuid=EXPECTED_UUID,
        name="Herobrine",
        verified_at=now() - verified_days_ago * 86400.0,
    )


class AuthTestCase(unittest.TestCase):
    def service(self, http=None, store=None) -> AuthService:
        return AuthService(http or FakeHttp(), store)

    def login(self, http: FakeHttp) -> tuple[Account, list[str], FakeStore]:
        store = FakeStore()
        messages: list[str] = []
        account = self.service(http, store).wait_login(
            DeviceFlow("device-code-1", "ABCD-EFGH", "https://www.microsoft.com/link", 900, 0),
            FakeCancel(),
            messages.append,
        )
        return account, messages, store


# ---------------------------------------------------------------- 设备码流程
class TestDeviceFlow(AuthTestCase):
    def test_start_login_success(self):
        http = FakeHttp([_device_resp()])
        flow = self.service(http).start_login()
        self.assertIsInstance(flow, DeviceFlow)
        self.assertEqual(flow.device_code, "device-code-1")
        self.assertEqual(flow.user_code, "ABCD-EFGH")
        self.assertEqual(flow.verification_uri, "https://www.microsoft.com/link")
        self.assertEqual(flow.expires_in, 900)
        self.assertEqual(flow.interval, 0)
        call = http.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], DEVICE_CODE_URL)
        self.assertEqual(call["data"]["client_id"], auth_module.MSA_CLIENT_ID)
        self.assertEqual(call["data"]["scope"], "XboxLive.signin offline_access")
        self.assertEqual(call["data"]["response_type"], "device_code")

    def test_start_login_defaults_verification_uri(self):
        body = {"device_code": "dc", "user_code": "UC", "expires_in": 900, "interval": 5}
        flow = self.service(FakeHttp([_resp(body=body)])).start_login()
        self.assertEqual(flow.verification_uri, "https://www.microsoft.com/link")
        self.assertEqual(flow.interval, 5)

    def test_start_login_maps_invalid_client(self):
        http = FakeHttp([_resp(400, {"error": "invalid_client", "error_description": "bad cid"})])
        with self.assertRaises(AuthError) as ctx:
            self.service(http).start_login()
        self.assertEqual(ctx.exception.code, "invalid_client")
        self.assertIn("登录服务配置错误", ctx.exception.user_message)

    def test_start_login_maps_rate_limit_and_server_error(self):
        with self.assertRaises(AuthError) as ctx:
            self.service(FakeHttp([_resp(429, {})])).start_login()
        self.assertIn("请求过于频繁", ctx.exception.user_message)
        with self.assertRaises(AuthError) as ctx:
            self.service(FakeHttp([_resp(503, {})])).start_login()
        self.assertIn("暂时不可用", ctx.exception.user_message)
        self.assertEqual(ctx.exception.code, "http")

    def test_start_login_incomplete_body(self):
        with self.assertRaises(AuthError) as ctx:
            self.service(FakeHttp([_resp(body={"user_code": "UC"})])).start_login()
        self.assertEqual(ctx.exception.code, "protocol")

    def test_wait_login_full_chain(self):
        http = FakeHttp(_full_login_chain())
        account, messages, store = self.login(http)
        self.assertEqual(account.access_token, "mc-token-1")
        self.assertEqual(account.refresh_token, "msa-refresh-1")
        self.assertEqual(account.uuid, EXPECTED_UUID)  # 无横线、小写
        self.assertEqual(account.name, "Herobrine")
        self.assertGreater(account.verified_at, 0)
        self.assertTrue(account.token_valid())
        # 请求顺序：token → XBL → XSTS → MC → 所有权 → 档案（设备码由 start_login 单独请求）
        self.assertEqual(
            http.urls,
            [
                TOKEN_URL,
                XBL_USER_URL,
                XSTS_URL,
                MC_LOGIN_URL,
                MC_ENTITLEMENTS_URL,
                MC_PROFILE_URL,
            ],
        )
        # 中文阶段文案按顺序上报
        self.assertEqual(messages, [MSG_XBOX, MSG_XSTS, MSG_MC_LOGIN, MSG_ENTITLEMENTS, MSG_PROFILE])
        self.assertEqual(len(store.saved), 1)
        self.assertIs(store.saved[0], account)

    def test_identity_token_and_bearer_headers(self):
        http = FakeHttp(_full_login_chain())
        self.login(http)
        xbl_call = http.calls[1]
        self.assertEqual(xbl_call["json"]["Properties"]["RpsTicket"], "d=msa-access")
        self.assertEqual(xbl_call["json"]["RelyingParty"], "http://auth.xboxlive.com")
        self.assertEqual(xbl_call["headers"]["x-xbl-contract-version"], "1")
        xsts_call = http.calls[2]
        self.assertEqual(xsts_call["json"]["Properties"]["SandboxId"], "RETAIL")
        self.assertEqual(xsts_call["json"]["Properties"]["UserTokens"], ["xbl-token-1"])
        self.assertEqual(xsts_call["json"]["RelyingParty"], "rp://api.minecraftservices.com/")
        login_call = http.calls[3]
        self.assertEqual(login_call["json"]["identityToken"], "XBL3.0 x=uhs-abc;xsts-token-1")
        for call in http.calls[4:]:
            self.assertEqual(call["headers"]["Authorization"], "Bearer mc-token-1")


# ---------------------------------------------------------------- 轮询逻辑
class TestPolling(AuthTestCase):
    def test_pending_then_success(self):
        http = FakeHttp(
            [_resp(400, {"error": "authorization_pending"}), _resp(body=dict(MSA_BODY))]
            + _chain_after_msa()
        )
        cancel = FakeCancel()
        messages: list[str] = []
        account = self.service(http).wait_login(
            DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
            cancel,
            messages.append,
        )
        self.assertEqual(account.access_token, "mc-token-1")
        self.assertEqual(cancel.waits, [0.0])  # 间隔为 0，轮询一次
        self.assertIn(MSG_WAITING, messages)

    def test_slow_down_increases_interval(self):
        http = FakeHttp(
            [
                _resp(400, {"error": "authorization_pending"}),
                _resp(400, {"error": "slow_down"}),
                _resp(body=dict(MSA_BODY)),
            ]
            + _chain_after_msa()
        )
        cancel = FakeCancel()
        self.service(http).wait_login(
            DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0), cancel
        )
        # 第一次按 interval=0 等待，slow_down 后 +5 秒
        self.assertEqual(cancel.waits, [0.0, 5.0])

    def test_cancel_before_poll(self):
        http = FakeHttp()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CancelledError):
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0), cancel
            )
        self.assertEqual(http.calls, [])

    def test_cancel_during_wait(self):
        http = FakeHttp([_resp(400, {"error": "authorization_pending"})])
        with self.assertRaises(CancelledError):
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel([True]),
            )

    def test_expired_device_code(self):
        http = FakeHttp([_resp(400, {"error": "expired_token"})])
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "expired_token")
        self.assertIn("已过期", ctx.exception.user_message)

    def test_invalid_grant_must_include(self):
        http = FakeHttp([_resp(400, {"error": "invalid_grant", "error_description": "must include device_code"})])
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "protocol")

    def test_invalid_grant_other(self):
        http = FakeHttp([_resp(400, {"error": "invalid_grant", "error_description": "expired"})])
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "device_code")
        self.assertIn("重新开始登录", ctx.exception.user_message)

    def test_unknown_poll_error(self):
        http = FakeHttp([_resp(400, {"error": "weird_error"})])
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "weird_error")


# ---------------------------------------------------------------- 错误映射
class TestErrorMapping(AuthTestCase):
    def test_network_exception_mapped(self):
        http = FakeHttp([ConnectionError("proxy refused")])
        with self.assertRaises(AuthError) as ctx:
            self.service(http).start_login()
        self.assertEqual(ctx.exception.code, "network")
        self.assertIn("无法连接 Microsoft 登录服务器", ctx.exception.user_message)
        self.assertNotIsInstance(ctx.exception, (ConnectionError, OSError))
        self.assertIsNotNone(ctx.exception.__cause__)

    def test_xsts_xerr_json_body(self):
        http = FakeHttp(
            [
                _resp(body=dict(MSA_BODY)),
                _resp(body=dict(XBL_BODY)),
                _resp(401, {"XErr": 2148916233}),
            ]
        )
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "no_xbox_account")
        self.assertIn("尚未创建 Xbox 档案", ctx.exception.user_message)

    def test_xsts_xerr_header(self):
        http = FakeHttp(
            [
                _resp(body=dict(MSA_BODY)),
                _resp(body=dict(XBL_BODY)),
                _resp(401, {}, headers={"x-err": "2148916235"}),
            ]
        )
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "region_unsupported")
        self.assertIn("国家/地区不支持", ctx.exception.user_message)

    def test_xerr_6236_and_6237_semantics_per_auth_md(self):
        self.assertEqual(auth_module.XERR_MESSAGES[2148916236][1], "age_verification_required")
        self.assertIn("年龄验证", auth_module.XERR_MESSAGES[2148916236][0])
        self.assertEqual(auth_module.XERR_MESSAGES[2148916237][1], "playtime_limit")
        self.assertIn("游戏时长限制", auth_module.XERR_MESSAGES[2148916237][0])

    def test_unknown_xerr(self):
        http = FakeHttp(
            [_resp(body=dict(MSA_BODY)), _resp(body=dict(XBL_BODY)), _resp(401, {"XErr": 999999999})]
        )
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertIn("999999999", ctx.exception.user_message)
        self.assertEqual(ctx.exception.code, "xerr_999999999")

    def test_xbl_failure_without_xerr(self):
        http = FakeHttp([_resp(body=dict(MSA_BODY)), _resp(503, {}, text="busy")])
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertIn("Xbox 账号验证失败（HTTP 503）", ctx.exception.user_message)

    def test_xsts_missing_token(self):
        http = FakeHttp(
            [_resp(body=dict(MSA_BODY)), _resp(body=dict(XBL_BODY)), _resp(body={"Token": ""})]
        )
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "protocol")

    def test_uhs_mismatch(self):
        http = FakeHttp(
            [
                _resp(body=dict(MSA_BODY)),
                _resp(body=dict(XBL_BODY)),
                _resp(body={"Token": "t", "DisplayClaims": {"xui": [{"uhs": "other-uhs"}]}}),
            ]
        )
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "protocol")
        self.assertIn("不一致", ctx.exception.user_message)

    def test_mc_login_401(self):
        http = FakeHttp(
            [
                _resp(body=dict(MSA_BODY)),
                _resp(body=dict(XBL_BODY)),
                _resp(body=dict(XSTS_BODY)),
                _resp(401, {}),
            ]
        )
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "unauthorized")
        self.assertIn("身份令牌被拒绝", ctx.exception.user_message)

    def test_not_owned(self):
        http = FakeHttp(
            [_resp(body=dict(MSA_BODY))]
            + [_resp(body=dict(XBL_BODY)), _resp(body=dict(XSTS_BODY)), _resp(body=dict(MC_LOGIN_BODY))]
            + [_resp(body={"items": [{"name": "other_game"}]})]
        )
        with self.assertRaises(NotOwnedError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertIsInstance(ctx.exception, AuthError)
        self.assertIn("没有已购买", ctx.exception.user_message)

    def test_entitlements_401(self):
        http = FakeHttp(
            [_resp(body=dict(MSA_BODY))]
            + [_resp(body=dict(XBL_BODY)), _resp(body=dict(XSTS_BODY)), _resp(body=dict(MC_LOGIN_BODY))]
            + [_resp(401, {})]
        )
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "unauthorized")

    def test_profile_404(self):
        http = FakeHttp(
            [_resp(body=dict(MSA_BODY))]
            + [_resp(body=dict(XBL_BODY)), _resp(body=dict(XSTS_BODY)), _resp(body=dict(MC_LOGIN_BODY))]
            + [_resp(body=dict(ENTITLEMENTS_BODY)), _resp(404, {}, text="Not Found")]
        )
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "no_profile")
        self.assertIn("尚未创建游戏档案", ctx.exception.user_message)

    def test_profile_bad_uuid(self):
        http = FakeHttp(
            [_resp(body=dict(MSA_BODY))]
            + [_resp(body=dict(XBL_BODY)), _resp(body=dict(XSTS_BODY)), _resp(body=dict(MC_LOGIN_BODY))]
            + [_resp(body=dict(ENTITLEMENTS_BODY)), _resp(body={"id": "abc", "name": "X"})]
        )
        with self.assertRaises(AuthError) as ctx:
            self.service(http).wait_login(
                DeviceFlow("device-code-1", "UC", "https://www.microsoft.com/link", 900, 0),
                FakeCancel(),
            )
        self.assertEqual(ctx.exception.code, "protocol")


# ---------------------------------------------------------------- 30 天 / 刷新
class TestRefresh(AuthTestCase):
    def test_refresh_success_rotates_tokens(self):
        http = FakeHttp(_refresh_chain())
        store = FakeStore(reverify=False)
        account = _account(verified_days_ago=1, expires_in=-10)
        refreshed = self.service(http, store).refresh(account)
        self.assertEqual(refreshed.access_token, "mc-token-1")
        self.assertEqual(refreshed.refresh_token, "msa-refresh-1")  # 轮换后的新 refresh_token
        self.assertEqual(refreshed.uuid, EXPECTED_UUID)
        self.assertEqual(refreshed.name, "Herobrine")
        self.assertAlmostEqual(refreshed.verified_at, account.verified_at, places=3)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(store.reverify_calls[0][1], 30)
        self.assertEqual(http.calls[0]["url"], TOKEN_URL)
        self.assertEqual(http.calls[0]["data"]["grant_type"], "refresh_token")
        self.assertEqual(http.calls[0]["data"]["refresh_token"], "rt-old")
        self.assertEqual(http.urls[-1], MC_LOGIN_URL)

    def test_refresh_keeps_new_refresh_token_from_msa(self):
        body = dict(MSA_BODY, refresh_token="rotated-rt")
        http = FakeHttp([_resp(body=body)] + _chain_after_msa()[:3])
        refreshed = self.service(http, FakeStore()).refresh(_account(verified_days_ago=1))
        self.assertEqual(refreshed.refresh_token, "rotated-rt")

    def test_refresh_rejected_after_30_days(self):
        store = FakeStore(reverify=True)
        account = _account(verified_days_ago=31)
        with self.assertRaises(AuthError) as ctx:
            self.service(FakeHttp(), store).refresh(account)
        self.assertEqual(ctx.exception.code, "reverify")
        self.assertIn("30 天", ctx.exception.user_message)
        self.assertIn("重新使用设备码登录", ctx.exception.user_message)

    def test_refresh_fallback_30_day_math_when_store_none(self):
        account = _account(verified_days_ago=31)
        with self.assertRaises(AuthError) as ctx:
            self.service(FakeHttp(), None).refresh(account)
        self.assertEqual(ctx.exception.code, "reverify")

    def test_refresh_allowed_just_under_30_days_with_real_store_logic(self):
        # 29 天：允许刷新（此处用真实 AccountStore 的判定逻辑）
        from cubefetch.core.account_store import AccountStore

        class StoreAdapter(FakeStore):
            def __init__(self):
                super().__init__()
                self._real = AccountStore()

            def needs_reverify(self, account, days: int = 30) -> bool:
                return self._real.needs_reverify(account, days)

        http = FakeHttp(_refresh_chain())
        account = _account(verified_days_ago=29, expires_in=-10)
        refreshed = self.service(http, StoreAdapter()).refresh(account)
        self.assertEqual(refreshed.access_token, "mc-token-1")

    def test_refresh_without_refresh_token(self):
        account = _account()
        account.refresh_token = ""
        with self.assertRaises(AuthError) as ctx:
            self.service(FakeHttp(), FakeStore()).refresh(account)
        self.assertEqual(ctx.exception.code, "no_refresh_token")

    def test_refresh_invalid_grant(self):
        http = FakeHttp([_resp(400, {"error": "invalid_grant", "error_description": "revoked"})])
        with self.assertRaises(AuthError) as ctx:
            self.service(http, FakeStore()).refresh(_account(verified_days_ago=1))
        self.assertEqual(ctx.exception.code, "invalid_grant")
        self.assertIn("登录状态已失效", ctx.exception.user_message)

    def test_refresh_bad_account(self):
        with self.assertRaises(AuthError) as ctx:
            self.service(FakeHttp(), FakeStore()).refresh({"uuid": "x"})
        self.assertEqual(ctx.exception.code, "bad_account")


# ---------------------------------------------------------------- 身份复核
class TestVerifyIdentity(AuthTestCase):
    def test_verify_identity_success(self):
        http = FakeHttp([_resp(body=dict(ENTITLEMENTS_BODY)), _resp(body=dict(PROFILE_BODY))])
        account = _account(verified_days_ago=10)
        old_verified = account.verified_at
        self.service(http, FakeStore()).verify_identity(account)
        self.assertEqual(account.uuid, EXPECTED_UUID)
        self.assertEqual(account.name, "Herobrine")
        self.assertGreater(account.verified_at, old_verified)
        self.assertEqual(account.access_token, "mc-old")  # token 未过期则不改动
        self.assertEqual(len(http.calls), 2)

    def test_verify_identity_renews_expired_token_then_retries(self):
        http = FakeHttp(
            [
                # token 过期 → 先走 _renew_account（refresh 链），再拉所有权/档案
                *_refresh_chain(),
                _resp(body=dict(ENTITLEMENTS_BODY)),
                _resp(body=dict(PROFILE_BODY)),
            ]
        )
        account = _account(verified_days_ago=10, expires_in=-10)
        self.service(http, FakeStore()).verify_identity(account)
        self.assertEqual(account.access_token, "mc-token-1")
        self.assertGreater(account.verified_at, 0)

    def test_verify_identity_no_profile(self):
        http = FakeHttp([_resp(body=dict(ENTITLEMENTS_BODY)), _resp(404, {})])
        with self.assertRaises(AuthError) as ctx:
            self.service(http, FakeStore()).verify_identity(_account())
        self.assertEqual(ctx.exception.code, "no_profile")


# ---------------------------------------------------------------- 传输层兼容
class TestTransport(AuthTestCase):
    def test_uses_session_attribute(self):
        class SessionWrapper:
            def __init__(self):
                self.session = FakeHttp([_device_resp()])

        wrapper = SessionWrapper()
        service = AuthService(wrapper, FakeStore())
        flow = service.start_login()
        self.assertEqual(flow.user_code, "ABCD-EFGH")
        self.assertEqual(len(wrapper.session.calls), 1)

    def test_uses_request_method_directly(self):
        http = FakeHttp([_device_resp()])
        self.service(http).start_login()
        self.assertEqual(http.calls[0]["timeout"], 30.0)


if __name__ == "__main__":
    unittest.main()
