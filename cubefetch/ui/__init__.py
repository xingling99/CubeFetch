"""CubeFetch 界面包。

对外的入口是 :func:`create_app` / :func:`run` / :class:`AppWindow`，
供 ``cubefetch/app.py`` 直接 ``from .ui import create_app`` 使用。
"""

from __future__ import annotations

from .app_window import APP_TITLE, AppWindow, CubeFetchApp, create_app, run

__all__ = ["APP_TITLE", "AppWindow", "CubeFetchApp", "create_app", "run"]
