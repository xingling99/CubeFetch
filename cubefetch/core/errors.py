"""统一异常类型。

面向用户的文字放在 user_message（中文），内部细节放 detail，方便日志排查。
"""
from __future__ import annotations


class CubeFetchError(Exception):
    """所有 CubeFetch 业务异常的基类。"""

    default_user_message = "发生了一个未知错误"

    def __init__(self, user_message: str | None = None, detail: str = "") -> None:
        self.user_message = user_message or self.default_user_message
        self.detail = detail
        super().__init__(f"{self.user_message} ({detail})" if detail else self.user_message)


class NetworkError(CubeFetchError):
    default_user_message = "网络请求失败，请检查网络连接或切换下载镜像"


class AuthError(CubeFetchError):
    default_user_message = "登录失败"

    def __init__(self, user_message: str | None = None, detail: str = "", code: str = "") -> None:
        self.code = code
        super().__init__(user_message, detail)


class NotOwnedError(AuthError):
    default_user_message = "该 Microsoft 账号没有已购买的 Minecraft，无法使用下载功能"


class CancelledError(CubeFetchError):
    default_user_message = "操作已取消"


class InstallError(CubeFetchError):
    default_user_message = "安装失败"


class ConfigError(CubeFetchError):
    default_user_message = "配置读写失败"
