"""文件级完整性校验（客户端防破解）。

设计说明：

- 构建时由 ``build/gen_integrity.py`` 扫描 ``cubefetch/**/*.py`` 生成
  ``cubefetch/integrity_manifest.py``：

  * ``FILES``：相对仓库根（posix 分隔符）的源码 sha256，供源码模式校验；
  * ``CODE``：模块名 → 规范化 code object 的 sha256，供 PyInstaller 冻结模式校验。

- 运行时 :func:`verify` 自动选择模式：

  * **源码模式**（``sys.frozen`` 为假）：逐文件比对 ``FILES``。文件缺失、哈希不符、
    出现未登记的新 ``.py`` 文件都记为 problem。
  * **冻结模式**（PyInstaller onefile / onedir）：源码文件不落地，改用
    ``importlib`` 取得模块 code object，规范化后 ``marshal.dumps`` 再哈希，与
    ``CODE`` 比对。规范化用于消除 marshal 对字符串驻留/引用编码的差异（同一份
    代码在打包进程与源码进程里 ``marshal.dumps`` 的字节并不相同，但规范化后一致），
    并把 ``frozenset`` 常量排序成固定顺序的元组（集合迭代顺序受哈希随机化影响）。
    PyInstaller 会裁掉没有静态引用的死模块（从未被任何代码导入的模块不会进 PYZ），
    这类模块无法在冻结包里取到 code object，故列入
    ``IntegrityReport.skipped`` 而非 problem（否则正常打包也会被误报为篡改）；
    其余被打包模块照常逐个校验，哈希不符仍记为 problem。
  * **无清单**：``mode == "unverified"``，明确表示"未校验"，而不是"校验通过"。

- 客户端校验只能提高破解门槛，无法绝对防止破解：攻击者仍可反编译并替换校验逻辑。
  清单自身无法把自己纳入哈希（自引用），亦属已知限制。冻结模式只能覆盖被
  PyInstaller 实际打包的模块，被裁掉/未打包的模块属于可校验盲区（见上）。

本模块只依赖标准库，可被构建脚本和打包后的程序共同复用。
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import importlib
import importlib.util
import logging
import marshal
import os
import sys
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType
from typing import Any, Mapping

# 静态导入能够保证 PyInstaller 分析阶段把清单模块打进 PYZ（运行期导入是动态的）。
_MANIFEST_MODULE: Any = None
try:  # pragma: no cover - 仅打包/生成清单后才成立
    from .. import integrity_manifest as _MANIFEST_MODULE  # type: ignore[attr-defined]
except ImportError:  # 尚未生成清单（开发模式下首次运行）
    _MANIFEST_MODULE = None

log = logging.getLogger("cubefetch.integrity")

#: 包目录名（相对仓库根）
PACKAGE_NAME = "cubefetch"
#: 清单模块名
MANIFEST_MODULE = "cubefetch.integrity_manifest"
#: 清单文件相对仓库根路径（生成物，不纳入自身校验）
MANIFEST_REL_PATH = "cubefetch/integrity_manifest.py"

_MODE_SOURCE = "source"
_MODE_FROZEN = "frozen"
_MODE_UNVERIFIED = "unverified"

#: 清单缺失/无效时的提示（中文，直接展示给用户）
_NO_MANIFEST_MESSAGE = (
    "未找到文件完整性清单（cubefetch/integrity_manifest.py），无法校验程序文件；"
    "请运行 build/gen_integrity.py 重新生成"
)


# ================================================================== 数据模型
@dataclass
class IntegrityReport:
    """一次完整性校验的结果。

    - ``ok``：是否通过；``False`` 时 ``problems`` 非空。
    - ``mode``：``"source"``（源码）/ ``"frozen"``（打包）/ ``"unverified"``（无清单）。
    - ``problems``：中文问题描述列表（空列表表示无问题）。
    - ``skipped``：冻结模式中未能参与校验的模块（PyInstaller 裁包的死模块），
      不影响 ``ok``，但会在 :func:`summarize` 中提示，属于已知盲区。
    """

    ok: bool
    mode: str
    problems: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


# ================================================================== 哈希工具
def file_digest(data: bytes) -> str:
    """文件内容 sha256（十六进制小写）。"""
    return hashlib.sha256(data).hexdigest()


def _fresh(value: str) -> str:
    """返回内容相同、但为全新对象（非驻留）的字符串。

    marshal 会为"同一对象"输出引用标记，而字符串是否被驻留取决于对象身份，
    因此冻结侧与源码侧即便语义完全相同，字节也可能不同。统一换成全新对象即可消除。
    """
    return value.encode("utf-8", "surrogatepass").decode("utf-8", "surrogatepass")


def _canonical_const(value: Any) -> Any:
    """把 code 常量规范化成可稳定序列化的等价形式。"""
    if isinstance(value, CodeType):
        return canonicalize_code(value)
    if isinstance(value, str):
        return _fresh(value)
    if isinstance(value, tuple):
        return tuple(_canonical_const(item) for item in value)
    if isinstance(value, frozenset):
        # 集合迭代顺序受哈希随机化影响，打包/运行两侧可能不同 → 排序成固定元组。
        items = [_canonical_const(item) for item in value]
        items.sort(key=lambda item: (type(item).__name__, repr(item)))
        return tuple(items)
    return value


def canonicalize_code(code: CodeType) -> CodeType:
    """递归重建 code object，抹平 marshal 的字符串引用/驻留与集合顺序差异。"""
    return code.replace(
        co_consts=tuple(_canonical_const(item) for item in code.co_consts),
        co_names=tuple(_fresh(name) for name in code.co_names),
        co_varnames=tuple(_fresh(name) for name in code.co_varnames),
        co_freevars=tuple(_fresh(name) for name in code.co_freevars),
        co_cellvars=tuple(_fresh(name) for name in code.co_cellvars),
        co_filename=_fresh(code.co_filename),
        co_name=_fresh(code.co_name),
        co_qualname=_fresh(code.co_qualname),
    )


def code_digest(code: CodeType) -> str:
    """把 code object 规范化后序列化再取 sha256（冻结模式使用）。"""
    return file_digest(marshal.dumps(canonicalize_code(code)))


# ================================================================== 清单生成
def repo_root() -> Path:
    """本文件所在仓库根目录（``cubefetch/core/integrity.py`` → 仓库根）。"""
    return Path(__file__).resolve().parents[2]


def compiled_filename(rel_path: str) -> str:
    """编译源码时使用的文件名（与 PyInstaller 匿名化后的 ``co_filename`` 一致）。

    PyInstaller 会按模块名把 ``co_filename`` 重写为 ``os.path.join(*模块名分段, "xxx.py")``，
    形如 ``cubefetch\\core\\http.py``（Windows）。源码侧编译时直接用同样的相对路径即可对齐。
    """
    return rel_path.replace("/", os.sep)


def module_name(rel_path: str) -> str | None:
    """由相对仓库根的路径推导模块名；不是包内 ``.py`` 文件时返回 ``None``。"""
    rel = rel_path.replace("\\", "/")
    if not rel.endswith(".py"):
        return None
    parts = rel.split("/")
    if not parts or parts[0] != PACKAGE_NAME:
        return None
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    if not parts:
        return None
    return ".".join(parts)


def _iter_package_sources(root: Path) -> list[tuple[str, Path]]:
    """列出包内全部 ``.py`` 源码（排除 ``__pycache__`` 与清单生成物），按路径排序。"""
    package_dir = root / PACKAGE_NAME
    if not package_dir.is_dir():
        return []
    sources: list[tuple[str, Path]] = []
    for path in package_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel == MANIFEST_REL_PATH:
            continue
        sources.append((rel, path))
    sources.sort(key=lambda item: item[0])
    return sources


def _build_time(sources: list[tuple[str, Path]], build_time: str | None) -> str:
    """生成 ``BUILD_TIME``（UTC ISO8601 ``YYYY-MM-DDTHH:MM:SSZ``）。

    默认取所有源码文件的最新 mtime，保证同一份源码重复生成清单时结果完全一致
    （幂等）；也可用 ``SOURCE_DATE_EPOCH`` 环境变量或显式参数覆盖。
    """
    if build_time:
        return str(build_time)
    epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    timestamp: float | None = None
    if epoch:
        try:
            timestamp = float(epoch)
        except ValueError:
            timestamp = None
    if timestamp is None:
        mtimes = []
        for _rel, path in sources:
            try:
                mtimes.append(path.stat().st_mtime)
            except OSError:
                continue
        timestamp = max(mtimes) if mtimes else _time.time()
    return _datetime.datetime.fromtimestamp(
        timestamp, _datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def compile_module(data: bytes, rel_path: str) -> CodeType:
    """按与 PyInstaller 相同的方式编译模块源码。

    PyInstaller（spec 中 ``optimize=0``）用 ``compile(src, src_path, "exec", optimize=0)``
    编译后把 ``co_filename`` 重写为相对路径；这里直接以相对路径编译等价。
    ``dont_inherit=True`` 保证不受本文件 ``from __future__`` 影响。
    """
    return compile(
        data,
        compiled_filename(rel_path),
        "exec",
        optimize=0,
        dont_inherit=True,
    )


def build_manifest(
    root: Path | str | None = None,
    *,
    build_time: str | None = None,
) -> dict[str, Any]:
    """扫描包源码生成清单数据（``BUILD_TIME`` / ``FILES`` / ``CODE``）。"""
    root_path = Path(root).resolve() if root is not None else repo_root()
    sources = _iter_package_sources(root_path)
    files: dict[str, str] = {}
    code: dict[str, str] = {}
    for rel, path in sources:
        try:
            data = path.read_bytes()
        except OSError as exc:  # 读不到源码属于构建期错误，明确抛出
            raise RuntimeError(f"无法读取源码文件 {rel}：{exc}") from exc
        files[rel] = file_digest(data)
        name = module_name(rel)
        if name:
            code[name] = code_digest(compile_module(data, rel))
    return {
        "BUILD_TIME": _build_time(sources, build_time),
        "FILES": files,
        "CODE": code,
    }


def format_manifest_source(manifest: Mapping[str, Any]) -> str:
    """把清单数据渲染成 ``cubefetch/integrity_manifest.py`` 源码文本（确定性输出）。"""
    build_time = str(manifest.get("BUILD_TIME") or "")
    files = manifest.get("FILES") or {}
    code = manifest.get("CODE") or {}
    lines: list[str] = [
        "# -*- coding: utf-8 -*-",
        '"""CubeFetch 文件完整性清单（自动生成，请勿手动修改）。',
        "",
        "由 ``build/gen_integrity.py`` 生成：",
        "- ``FILES``：相对仓库根的源码文件 sha256（源码模式校验）；",
        "- ``CODE``：各模块规范化 code object 的 sha256（PyInstaller 冻结模式校验）。",
        "",
        "修改源码后必须重新运行 ``python -X utf8 build/gen_integrity.py``。",
        '"""',
        "",
        f'BUILD_TIME = "{build_time}"',
        "",
        "FILES = {",
    ]
    for rel in sorted(files):
        lines.append(f"    {rel!r}: {files[rel]!r},")
    lines.extend(["}", "", "CODE = {"])
    for name in sorted(code):
        lines.append(f"    {name!r}: {code[name]!r},")
    lines.extend(["}", ""])
    return "\n".join(lines)


# ================================================================== 校验
def _manifest_module() -> Any:
    """返回已加载的清单模块（优先静态导入，便于打包；否则动态导入）。"""
    if _MANIFEST_MODULE is not None:
        return _MANIFEST_MODULE
    try:
        return importlib.import_module(MANIFEST_MODULE)
    except Exception:  # noqa: BLE001 - 清单缺失/损坏都不应让调用方崩溃
        log.debug("无法导入完整性清单模块 %s", MANIFEST_MODULE, exc_info=True)
        return None


def _as_manifest_mapping(manifest: Any) -> dict[str, Any] | None:
    """把清单（模块或字典）规整为 ``{"FILES": dict, "CODE": dict}``；格式无效返回 None。"""
    if manifest is None:
        return None
    if isinstance(manifest, Mapping):
        files = manifest.get("FILES")
        code = manifest.get("CODE")
    else:
        files = getattr(manifest, "FILES", None)
        code = getattr(manifest, "CODE", None)
    if not isinstance(files, Mapping) or not isinstance(code, Mapping):
        return None
    if not files and not code:
        return None
    return {"FILES": dict(files), "CODE": dict(code)}


def _module_code(name: str) -> CodeType | None:
    """用 importlib 取得模块 code object（冻结模式下由 ``PyiFrozenLoader`` 提供）。"""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError):
        spec = None
    if spec is not None and spec.loader is not None:
        get_code = getattr(spec.loader, "get_code", None)
        if callable(get_code):
            try:
                code = get_code(name)
            except Exception:  # noqa: BLE001 - 继续尝试导入模块兜底
                code = None
            if code is not None:
                return code
    try:
        module = importlib.import_module(name)
    except Exception:  # noqa: BLE001
        return None
    loader = getattr(module, "__loader__", None)
    get_code = getattr(loader, "get_code", None)
    if callable(get_code):
        try:
            return get_code(name)
        except Exception:  # noqa: BLE001
            return None
    return None


def _verify_source(root: Path, files: Mapping[str, str]) -> list[str]:
    """源码模式：逐文件校验 sha256，并找出未登记的新源码文件。"""
    problems: list[str] = []
    if not root.is_dir():
        return [f"源码根目录不存在：{root}"]
    for rel in sorted(files):
        expected = files[rel]
        if not isinstance(rel, str) or not isinstance(expected, str):
            problems.append("完整性清单包含非法条目（应为 str → str）")
            continue
        path = root / rel
        if not path.is_file():
            problems.append(f"文件缺失：{rel}")
            continue
        try:
            actual = file_digest(path.read_bytes())
        except OSError as exc:
            problems.append(f"无法读取文件：{rel}（{exc}）")
            continue
        if actual != expected.lower():
            problems.append(f"文件被修改：{rel}")
    for rel, _path in _iter_package_sources(root):
        if rel not in files:
            problems.append(f"未登记文件：{rel}")
    return problems


def _verify_frozen(code_map: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """冻结模式：逐模块比对规范化 code object 哈希。

    返回 ``(problems, skipped)``。``skipped`` 是打包产物中不存在的模块——
    PyInstaller 会裁掉没有静态引用的模块，它们无法参与校验，但不应当作篡改。
    """
    problems: list[str] = []
    skipped: list[str] = []
    for name in sorted(code_map):
        expected = code_map[name]
        if not isinstance(name, str) or not isinstance(expected, str):
            problems.append("完整性清单 CODE 包含非法条目（应为 str → str）")
            continue
        code = _module_code(name)
        if code is None:
            skipped.append(name)
            continue
        if code_digest(code) != expected.lower():
            problems.append(f"模块代码被修改：{name}")
    return problems, skipped


def verify(
    manifest: Any = None,
    *,
    root: Path | str | None = None,
) -> IntegrityReport:
    """执行完整性校验并返回 :class:`IntegrityReport`。

    - ``manifest``：默认 ``None`` 表示加载 ``cubefetch.integrity_manifest``；
      测试可显式传入字典或模块对象。
    - ``root``：源码模式下的仓库根目录，默认本文件所在仓库根。

    无清单时返回 ``mode="unverified"``（``ok=False``，明确未校验而非通过）。
    """
    mode = _MODE_FROZEN if getattr(sys, "frozen", False) else _MODE_SOURCE
    raw = manifest if manifest is not None else _manifest_module()
    if raw is None:
        return IntegrityReport(ok=False, mode=_MODE_UNVERIFIED, problems=[_NO_MANIFEST_MESSAGE])
    data = _as_manifest_mapping(raw)
    if data is None:
        return IntegrityReport(
            ok=False,
            mode=_MODE_UNVERIFIED,
            problems=["文件完整性清单格式无效（缺少 FILES/CODE），请重新生成清单"],
        )
    skipped: list[str] = []
    if mode == _MODE_FROZEN:
        problems, skipped = _verify_frozen(data["CODE"])
    else:
        root_path = Path(root).resolve() if root is not None else repo_root()
        problems = _verify_source(root_path, data["FILES"])
    return IntegrityReport(ok=not problems, mode=mode, problems=problems, skipped=skipped)


def summarize(report: IntegrityReport) -> str:
    """把校验结果转成可读的中文摘要（用于日志与界面提示）。"""
    mode_text = {
        _MODE_SOURCE: "源码模式",
        _MODE_FROZEN: "冻结模式（打包）",
        _MODE_UNVERIFIED: "未校验",
    }.get(report.mode, report.mode)
    if report.ok:
        text = f"程序文件完整性校验通过（{mode_text}）"
        if report.skipped:
            text += f"；{len(report.skipped)} 个未打包模块未参与校验（已裁包，属已知盲区）"
        return text
    total = len(report.problems)
    lines = [f"程序文件完整性校验失败（{mode_text}，共 {total} 处异常）："]
    shown = report.problems[:8]
    lines.extend(f"- {item}" for item in shown)
    if total > len(shown):
        lines.append(f"- ……其余 {total - len(shown)} 处省略")
    if report.skipped:
        lines.append(f"- 另有 {len(report.skipped)} 个未打包模块未参与校验（已裁包，属已知盲区）")
    return "\n".join(lines)


__all__ = [
    "IntegrityReport",
    "MANIFEST_MODULE",
    "MANIFEST_REL_PATH",
    "PACKAGE_NAME",
    "build_manifest",
    "canonicalize_code",
    "code_digest",
    "compile_module",
    "compiled_filename",
    "file_digest",
    "format_manifest_source",
    "module_name",
    "repo_root",
    "summarize",
    "verify",
]
