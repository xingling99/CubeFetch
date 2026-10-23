# CubeFetch（方块速递）— 模块契约 v1

所有实现者必须严格遵守本文件中的路径、类名、函数签名。契约变更须由 Lead 修改本文件并通知所有实现者。

## 0. 全局约定

- 语言：Python 3.13，Windows 优先（代码需在 Windows 上运行，可容忍 Linux 分支）。
- 第三方依赖：**仅 `requests`**。其余一律标准库。禁止新增依赖（打包体积要求）。
- 所有模块使用 `from __future__ import annotations` + 类型注解。
- 日志用 `logging.getLogger("cubefetch.<module>")`，禁止 `print`（`tests/` 与 `app.py` 的调试输出除外）。
- 面向用户的错误信息通过 `errors.CubeFetchError.user_message`（中文）传递；内部细节放 `detail`。
- 长耗时操作必须在工作线程执行；UI 线程只做界面。取消用 `threading.Event`（已 `set()` 表示取消）。
- 进度回调签名为 `Callable[[Progress], None]`，可在任意线程被调用。
- 时间戳统一用 `time.time()` 的 float（Unix 秒）。
- 文件编码统一 UTF-8。路径一律使用 `pathlib.Path`。

## 1. 目录结构

```
CubeFetch/
  app.py                  # 入口（Lead 负责）
  requirements.txt
  cubefetch/
    __init__.py           # __version__
    config.py             # AppConfig / 路径
    services.py           # AppServices 门面（Lead 负责）
    core/
      __init__.py
      errors.py           # Lead 已写
      util.py             # Lead 已写
      http.py             # [install-engine]
      manifest.py         # [install-engine]
      install.py          # [install-engine]
      loader.py           # [loader-integration]
      detect.py           # [loader-integration]
      isolation.py        # [loader-integration]
      auth.py             # [auth-core]
      account_store.py    # [auth-core]
      security.py         # [auth-core]
    ui/
      __init__.py
      theme.py            # [ui]
      widgets.py          # [ui]
      app_window.py       # [ui]
      pages/__init__.py
      pages/login.py      # [ui]
      pages/versions.py   # [ui]
      pages/loaders.py    # [ui]
      pages/progress.py   # [ui]
      pages/done.py       # [ui]
  tests/                  # 各模块自带单测，pytest 兼容也可用 unittest
  build/build.ps1         # 打包脚本（Lead 稍后）
  docs/research/*.md      # 研究结果
```

## 2. 已完成的基础模块（直接使用，勿改）

### `cubefetch/core/errors.py`
```python
class CubeFetchError(Exception):
    def __init__(self, user_message: str, detail: str = ""): ...
class NetworkError(CubeFetchError)
class AuthError(CubeFetchError)          # .code: str
class NotOwnedError(AuthError)           # 账号没有 Minecraft 所有权
class CancelledError(CubeFetchError)
class InstallError(CubeFetchError)
class ConfigError(CubeFetchError)
```

### `cubefetch/core/util.py`（签名已固定）
```python
def sha1_file(path: Path) -> str
def sha256_file(path: Path) -> str
def human_size(n: int) -> str
def ensure_dir(p: Path) -> Path
def atomic_write_bytes(path: Path, data: bytes) -> None
def atomic_write_text(path: Path, text: str) -> None
def read_json(path: Path, default=None)
def write_json(path: Path, obj) -> None
def now() -> float
def find_java_candidates() -> list[Path]          # PATH + 常见安装目录 + PCL 配置
def java_major_version(java_exe: Path) -> int|None  # 运行 `java -version` 解析
def is_admin() -> bool
def open_in_explorer(path: Path) -> None
```

### `cubefetch/config.py`
```python
APP_NAME = "CubeFetch"
APP_NAME_CN = "方块速递"
APP_VERSION = "1.0.0"

def appdata_dir() -> Path      # %LOCALAPPDATA%/CubeFetch（已创建）
def cache_dir() -> Path        # appdata_dir()/cache
def log_dir() -> Path

@dataclass
class AppConfig:
    mc_root: str | None = None
    launcher: str = "auto"        # auto|pcl|hmcl|custom
    mirror: str = "auto"          # auto|official|bmclapi
    threads: int = 32
    remember_days: int = 30
    java_path: str | None = None
    last_version: str | None = None
    last_loader: str | None = None
    close_to_tray: bool = False    # 预留，不必实现
    def save(self) -> None: ...
    @classmethod
    def load(cls) -> "AppConfig": ...
```

## 3. `core/http.py` — [install-engine]

```python
@dataclass
class Progress:
    # v1.1.1（task-14）口径：done/total 永远是**整体文件数**（静态+资源）；
    # bytes_* 永远是**整体字节数**；stage_* 只描述当前阶段；message 只放文案（不含数字分母）。
    done: int = 0          # 整体已完成文件数
    total: int = 0         # 整体文件总数
    bytes_done: int = 0
    bytes_total: int = 0
    speed: float = 0.0     # bytes/s（整体）
    message: str = ""      # 当前阶段文案（中文，不含分母）
    stage: str = ""        # 当前阶段名，如「正在下载资源文件」
    stage_done: int = 0    # 阶段内已完成数
    stage_total: int = 0   # 阶段内总数；0 表示本阶段无细分进度（UI 按 indeterminate 显示）

class HttpSession:
    def __init__(self, mirror: str = "auto", timeout: int = 25, retries: int = 3, user_agent: str = "CubeFetch/1.0"): ...
    def get_json(self, url: str, *, use_mirror: bool = True) -> dict
    def get_bytes(self, url: str, *, use_mirror: bool = True) -> bytes
    def download(self, url: str, dest: Path, *, sha1: str | None = None, size: int | None = None,
                 use_mirror: bool = True, timeout: int | None = None) -> None
        # 已存在且校验通过则直接返回；失败自动重试（最多 retries）；
        # auto 模式**先镜像（BMCLAPI）后官方**，镜像不可用时回落官方（v1.0.1 起）

@dataclass
class DownloadTask:
    url: str
    dest: Path
    sha1: str | None = None
    size: int | None = None
    use_mirror: bool = True

class ThreadedDownloader:
    def __init__(self, http: HttpSession, workers: int = 32)
    def run(self, tasks: list[DownloadTask], *, progress_cb: Callable[[Progress], None] | None = None,
            cancel: threading.Event | None = None, stage: str = "") -> None
        # 工作线程池并发；单个任务失败→重试2次→抛出 NetworkError；cancel.set() 后尽快抛 CancelledError
        # v1.1.1：首事件 done=0；回调携带 stage/stage_done/stage_total；
        # 未知大小的任务会自适应上调 bytes_total；结束时强制发一次 done==total 且 bytes_done==bytes_total 的事件
```

镜像重写规则（`mirror="auto"` 时**先镜像、镜像不可用再回落官方**；`mirror="official"` 纯官方；`mirror="bmclapi"`/`"mirror"` 纯镜像）：
- `piston-meta.mojang.com` / `launchermeta.mojang.com` / `piston-data.mojang.com` / `launcher.mojang.com` → `https://bmclapi2.bangbang93.com`（保留 path）
- `https://libraries.minecraft.net/` → `https://bmclapi2.bangbang93.com/maven/`
- `https://resources.download.minecraft.net/` → `https://bmclapi2.bangbang93.com/assets/`
- `https://meta.fabricmc.net/` → `https://bmclapi2.bangbang93.com/fabric-meta/`
- `https://maven.fabricmc.net/` → `https://bmclapi2.bangbang93.com/maven/`
- `https://maven.neoforged.net/releases/` → `https://bmclapi2.bangbang93.com/maven/`（实测直连 maven.neoforged.net 超时，bmclapi 该 path 返回 200）
- 其余 URL 不重写。
- 注意：bmclapi 镜像必须**保留完整 path**（实测 `https://bmclapi2.bangbang93.com/version_manifest_v2.json` = 404，`.../mc/game/version_manifest_v2.json` = 200）。

## 4. `core/manifest.py` — [install-engine]

```python
CATEGORY_RELEASE = "release"     # 正式版
CATEGORY_SNAPSHOT = "snapshot"   # 预览版
CATEGORY_OLD = "old"             # 远古版
CATEGORY_APRIL = "aprilfool"     # 愚人节版

@dataclass(frozen=True)
class VersionEntry:
    id: str
    type: str            # Mojang 原始 type: release/snapshot/old_beta/old_alpha
    url: str
    release_time: str    # ISO 8601
    category: str        # 上述 4 类之一
    def release_date(self) -> datetime: ...

class ManifestService:
    def __init__(self, http: HttpSession, cache_ttl: int = 3600)
    def load(self) -> list[VersionEntry]           # 有缓存用缓存（cache_dir()/version_manifest.json），过期则刷新
    def refresh(self) -> list[VersionEntry]
    def categories(self) -> dict[str, list[VersionEntry]]   # 4 个 key，均按 release_time 倒序
    def fetch_version_json(self, version_id: str) -> dict    # 带缓存 cache_dir()/versions/<id>.json
```
愚人节版识别：硬编码清单（来自 Mojang manifest 实测）+
```
15w14a, 1.RV-Pre1, 3D Shareware v1.34, 20w14infinite, 22w13oneblockatatime,
23w13a_or_b, 24w14potato, 25w14craftmine, 26w14a
```
并支持 `docs/research/ecosystem.md` 中补充的条目；`type` 为 `old_beta/old_alpha` → `old`。

## 5. `core/install.py` — [install-engine]

```python
@dataclass
class InstallRequest:
    mc_root: Path
    version_id: str                  # 目标 versions/<id> 目录名
    version_json: dict               # 已解析（含 inheritsFrom 的版本也要能处理）
    workers: int = 32
    progress_cb: Callable[[Progress], None] | None = None
    cancel: threading.Event | None = None

class Installer:
    def __init__(self, http: HttpSession, manifest: ManifestService)
    def plan(self, req: InstallRequest) -> list[DownloadTask]     # 纯函数，可单测
    def install(self, req: InstallRequest) -> Path                 # 返回 versions/<id> 路径
```
必需行为：
1. 解析 `inheritsFrom` 链（loader profile 继承原版），合并 libraries / arguments / mainClass。
2. libraries 规则求值：`rules` 的 os.name/arch/version 匹配、`allow/disallow` 语义；`natives` classifier 与 extract(exclude)；下载到 `libraries/`；natives 解压到 `versions/<id>/<id>-natives/`（校验 exclude 的 META-INF）。
3. 客户端 jar：`downloads.client` → `versions/<id>/<id>.jar`（若目标 version 是 profile 且有 inheritsFrom，则 jar 放在父版本目录，子版本 json 里保留 inheritsFrom）。
4. 资源索引：`downloads.<platform>.url` → `assets/indexes/<assetIndex.id>.json`；objects → `assets/objects/<前2位>/<hash>`，多线程。
5. legacy 虚拟资源（`assetIndex.virtual == true` 或 `assets/minecraft` 缺失的旧版）：把 objects 复制/硬链到 `assets/virtual/legacy/`（或 `assets/virtual/<assetId>/`）。
6. logging 配置 `downloads.client.url` → `assets/log_configs/<id>`。
7. 写 `versions/<id>/<id>.json`（内容=version_json，UTF-8）。
8. 失败可重试；`cancel` 生效；进度经 `progress_cb` 上报（阶段文案中文）。
9. 目标版本已存在时校验关键文件，缺失才补下。

## 6. `core/detect.py` — [loader-integration]

```python
@dataclass
class LauncherProbe:
    kind: str                  # "pcl" | "hmcl"
    installed: bool
    exe_hint: Path | None
    config_path: Path | None
    mc_root: Path | None       # 从配置解析出的 .minecraft 根目录
    managed_versions: int = 0  # 该根目录下 versions 数量

@dataclass
class DetectionResult:
    probes: list[LauncherProbe]
    candidate_roots: list[Path]      # 去重后的候选根目录（存在的）
    default_root: Path               # 默认写入目标
    needs_choice: bool               # True 时 UI 必须弹窗让用户选
    reason: str                      # 中文说明，如 "检测到 PCL 与 HMCL 两个启动器"
    # v1.1.1（task-12）：主动扫描「有效安装根」——判据是 <root>/versions/<id>/<id>.json 至少 1 个。
    # 按版本数降序、去重；UI 设置页显示为 "D:\\pcl2\\.minecraft（3 个版本）"（0 版本的弱候选只显示路径）。
    root_versions: list[tuple[Path, int]] = field(default_factory=list)
    @property
    def root_version_counts(self) -> dict[Path, int]: ...

class LauncherDetector:
    def detect(self) -> DetectionResult
```
规则：单启动器→默认其根目录（`needs_choice=False`）；双启动器→`needs_choice=True`（候选含两者）；都没有→默认 `%APPDATA%\.minecraft`，`needs_choice=False`。PCL/HMCL 配置路径与键名严格按 `docs/research/ecosystem.md`。
v1.1.1 起：配置解析出的根仍作为优先候选；此外扫描 `%APPDATA%\.minecraft`、启动器目录下 `.minecraft`、桌面/下载/文档及固定盘符一级目录中的 `.minecraft`（`_MAX_SCAN_CHILDREN` 限流、不深度递归、跳过系统/无权限目录，单次 detect 控制在百毫秒级）。`default_root` 优先选「有效安装根中版本数最多者」；`candidate_roots` 有效安装根在前，空 `.minecraft` 只作弱候选。

## 7. `core/isolation.py` — [loader-integration]

```python
class VersionIsolation:
    @staticmethod
    def apply(mc_root: Path, version_id: str, launcher_kinds: list[str], enabled: bool = True) -> list[str]
        # 返回已写入的说明列表；按启动器写响应配置（PCL: versions/<id>/PCL/Setup.ini 等；HMCL: 对应机制）
    @staticmethod
    def is_isolated(mc_root: Path, version_id: str, launcher_kinds: list[str]) -> bool
```
具体键名以 `docs/research/ecosystem.md` 为准；对未安装的启动器跳过。**不得破坏用户已有配置**（读改写，保留其他键）。

## 8. `core/loader.py` — [loader-integration]

```python
@dataclass
class LoaderBuild:
    kind: str             # "vanilla"|"fabric"|"quilt"|"forge"|"neoforge"
    version: str | None   # loader 版本；vanilla 为 None
    game_version: str
    profile_id: str       # 最终 versions/<id> 目录名
    version_json: dict | None      # fabric/quilt 直接生成
    needs_installer: bool = False  # forge/neoforge 需运行安装器

class LoaderService:
    def __init__(self, http: HttpSession, installer: Installer, manifest: ManifestService)
    def list_loaders(self, kind: str, game_version: str) -> list[dict]
        # [{version, stable, ...}]；vanilla 返回 []
    def recommend(self, kind: str, game_version: str) -> str | None
    def prepare(self, kind: str, loader_version: str | None, game_version: str) -> LoaderBuild
    def install(self, build: LoaderBuild, mc_root: Path, *, java_exe: Path | None = None,
                progress_cb=None, cancel=None) -> str   # 返回 profile_id
```
Forge/NeoForge：先装 vanilla，再下载对应 installer.jar 并用 Java 无头运行（参数以研究结论为准），最后校验 `versions/<profile_id>/<profile_id>.json` 存在。

## 9. `core/auth.py` + `core/account_store.py` + `core/security.py` — [auth-core]

### auth.py
```python
@dataclass
class DeviceFlow:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int

@dataclass
class Account:
    access_token: str
    refresh_token: str
    expires_at: float
    uuid: str
    name: str
    xuid: str | None = None
    verified_at: float = 0.0      # 完整链最近一次验证成功时间
    def token_valid(self) -> bool

class AuthService:
    def __init__(self, http: HttpSession, store: "AccountStore")
    def start_login(self) -> DeviceFlow
    def wait_login(self, flow: DeviceFlow, cancel: threading.Event,
                   status_cb: Callable[[str], None] | None = None) -> Account
        # 轮询 token → XBL → XSTS → login_with_xbox → entitlements → profile
        # 抛出 AuthError / NotOwnedError / CancelledError
    def refresh(self, account: Account) -> Account            # 静默刷新
    def verify_identity(self, account: Account) -> None        # 重新拉 entitlements+profile，更新 verified_at
```
错误映射（中文 user_message）：无 Xbox 账号（2148916233）、未成年账号（2148916238）、地区不支持（2148916235）等。

### account_store.py
```python
class AccountStore:
    def __init__(self, path: Path | None = None)   # 默认 appdata_dir()/account.dat
    def load(self) -> Account | None
    def save(self, account: Account) -> None
    def clear(self) -> None
    def days_since_verified(self, account: Account) -> float
    def needs_reverify(self, account: Account, days: int = 30) -> bool
```
- 落盘必须用 Windows DPAPI（`CryptProtectData`，ctypes 调 crypt32，无需 pywin32）+ 完整性 HMAC；非 Windows 回退为明文+HMAC 并标记不安全。
- 到期判定：`now - verified_at > remember_days*86400` → 强制重新走设备码登录（refresh token 也不行）。

### security.py
```python
class AuthGate:
    """下载/安装的唯一入口守卫。只有 AuthService 能签发 Ticket。"""
    def issue(self, account: Account) -> "Ticket"
    def verify(self, ticket: "Ticket") -> Account   # 失败抛 AuthError

@dataclass(frozen=True)
class Ticket:
    uuid: str
    name: str
    expires_at: float
    mac: str          # HMAC-SHA256，密钥由 machine_id + 内置盐派生

def machine_id() -> str          # 注册表 MachineGuid 或卷序列号，稳定且不涉及隐私外发
def require_ticket(ticket: Ticket | None) -> Account   # Installer 入口调用
class IntegrityGuard:
    def check(self) -> list[str]     # 返回异常列表；空=通过
```
`install.py` 的 `install()` 必须调用 `security.require_ticket()`，UI 无法绕过（构造 Ticket 需要 mac）。

## 10. `ui/` — [ui]

- 纯 tkinter/ttk，无第三方 GUI 依赖；深色简约风，窗口 980x660，可缩放，最小 860x560。
- `AppWindow(master, services: AppServices)`：左侧导航 + 右侧页面容器；页面间切换用 `show_page(name)`。
- 页面：`login`（设备码登录）、`versions`（四分类版本列表 + 搜索）、`loaders`（加载器选择）、`progress`（安装进度）、`done`（完成）、`settings`（设置：镜像/线程/记住天数/Java）。
- 所有耗时操作通过 `services` 在工作线程执行，UI 用 `queue.Queue` + `after(50)` 轮询更新。
- 字体优先 "Microsoft YaHei UI"；主题色定义在 `ui/theme.py`（背景 #17171c、卡片 #22222b、主色 #6c5ce7、文字 #e8e8ef）。
- 不使用图片资源（体积优先），LOGO 用 Canvas 绘制或纯文字。

## 11. `services.py` — [Lead]
```python
class AppServices:
    def __init__(self, config: AppConfig | None = None): ...
    config: AppConfig
    http: HttpSession
    manifest: ManifestService
    auth: AuthService
    store: AccountStore
    detector: LauncherDetector
    installer: Installer
    loaders: LoaderService
    gate: AuthGate
    account: Account | None
    def bootstrap(self) -> None            # 加载配置、检测启动器、尝试恢复账号
    def login_flow(self) -> DeviceFlow
    def finish_login(self, flow, cancel, status_cb) -> Account
```

## 12. 测试要求

每个实现者至少提供 `tests/test_<module>.py`：
- install: 规则求值、natives 选择、legacy 资源、inheritsFrom 合并（用真实 1.20.1 / 1.7.10 / fabric profile JSON 的裁剪样本）。
- manifest: 四分类、愚人节清单、缓存。
- detect: 构造临时目录模拟 PCL/HMCL 配置，断言 needs_choice 规则。
- auth: 错误映射、30 天判定（不联网）。
- store: 加密往返、篡改检测（Windows DPAPI 可用时）。

## 13. 协作约定

- 只改自己负责的文件；需要改他人文件的，发消息给 Lead。
- 接口与契约冲突时以本文件为准；确有问题的，先报告再改。
- 每完成一块，运行 `python -m compileall` 与自己的单测，在任务完成说明里附上测试结果。

## 14. v1.1.0-beta 新增能力（用户第 3 轮需求，2026-10-21）

### 14.1 文件级防破解（task-10，auth-dev）
- `build/gen_integrity.py`：构建前扫描 `cubefetch/**/*.py`，生成 `cubefetch/integrity_manifest.py`（`FILES` 相对路径→sha256、`CODE` 模块名→marshal 代码哈希，排除清单自身）。
- `cubefetch/core/integrity.py`：
  - `IntegrityReport(ok: bool, mode: str, problems: list[str])`
  - `verify(manifest=None) -> IntegrityReport`：源码模式按 `FILES` 校验磁盘文件；冻结模式（PyInstaller）按 `CODE` 校验模块代码对象哈希。
  - `summarize(report) -> str`
- `cubefetch/core/security.py` 追加：`set_integrity_problems()` / `integrity_problems()`；存在问题时 `AuthGate.require_active()`、模块级 `require_active()`/`require_ticket()` 抛 `AuthError(code="integrity")`。
- 边界：客户端完整性校验只能提高篡改门槛，不能绝对防破解；`app.py` 启动时执行并在失败时警示 + 禁用下载门禁。

### 14.2 官方 Java 运行时下载（task-9，install-dev）
- `cubefetch/core/java_runtime.py`：
  - `JAVA_RUNTIME_INDEX_URL`（Mojang java-runtime all.json）、`PLATFORM = "windows-x64"`
  - `JavaRuntimeInfo(component, platform, version_name)`
  - `JavaRuntimeService(http, mc_root)`：`runtime_root()`（`<mc_root>/runtime`）、`find_installed(component)`（`.../<component>/<platform>/<component>/bin/java.exe`）、`resolve(required_major=None, version_json=None)`、`plan(component) -> list[DownloadTask]`、`install(component, *, progress_cb=None, cancel=None) -> Path`。
  - 安装布局按官方启动器：`<mc_root>/runtime/<component>/windows-x64/<component>/...`。

### 14.3 版本重命名（task-11，loader-dev）
- `cubefetch/core/version_rename.py`：
  - `validate_version_name(name) -> str`（禁止 `\ / : * ? " < > |`、控制字符、`.`/`..`、尾随点/空格、>64 字符）
  - `rename_installed_version(mc_root, old_id, new_name) -> str`：移动目录、改 version json 的 `id`、同步其它版本 json 的 `inheritsFrom` 引用。
  - `existing_version(mc_root, kind, game_version, loader_version) -> str | None`。

### 14.4 services 扩展（Lead）
```python
def java_status(self, game_version: str) -> dict
    # {"required_major": int|None, "java_path": Path|None, "component": str|None, "need_download": bool}，不联网
def download_java(self, game_version: str, progress_cb=None, cancel=None) -> Path
def existing_version(self, kind, loader_version, game_version) -> str | None
def install(self, kind, loader_version, game_version, progress_cb=None, cancel=None,
            custom_version_name: str | None = None) -> str
```
- `install()` 在 `loaders.install()` 成功后、写版本隔离之前执行重命名。
- 下载的 Java 会写入 `config.java_path` 并作为后续安装使用的 Java。

### 14.5 UI 行为（task-8，ui-dev）
- 下载页每次进程启动后首次进入调用 `versions_categories(refresh=True)` 强制联网刷新版本列表。
- 「开始下载」前：`java_status()` 缺 Java 时弹窗询问是否下载官方运行时；`existing_version()` 命中时弹窗让用户输入新版本名（留空沿用现有版本）。
- 进度页支持 `download_java=True` / `custom_version_name=...` 参数，Java 下载阶段文案「正在下载 Java 运行时…」。
- 提醒音效：`cubefetch/ui/sound.py` `play(kind)`（info/warn/error/success，Windows winsound，非 Windows 静默）。
- 关闭确认：有进行中任务时 `request_close()` 先弹「关闭后进度不保留」确认。

### 14.6 启动接线（Lead）
- `app.py` 启动时：`core.integrity.verify()` + `IntegrityGuard().check()` → `security.set_integrity_problems()`；有问题时状态栏警示 + 弹窗说明已禁用下载。
- `build/build.ps1` 在 PyInstaller 前自动运行 `build/gen_integrity.py`。

## 15. v1.1.1（2026-10-22，用户反馈 m01768）新增/变更

### 15.1 启动器检测改为按「有效安装根」扫描（task-12，loader-dev）
- `DetectionResult` 新增 `root_versions: list[tuple[Path, int]]`（默认空列表，向后兼容）与只读属性 `root_version_counts: dict[Path, int]`。
- 有效安装根判据：`<root>/versions/<id>/<id>.json` 至少 1 个；`candidate_roots` 有效根在前、空 `.minecraft` 降为弱候选；`default_root` 取版本数最多的有效根。
- 保留原有 PCL2/PCLCE/HMCL 配置解析（作为优先候选）；扫描上限 `_MAX_SCAN_CHILDREN=400`，不深度递归。
- UI（task-13）：设置页检测结果显示为 `路径（N 个版本）`，仍不显示启动器品牌名。

### 15.2 DPI 缩放（task-13，ui-dev）
- 新增 `cubefetch/ui/dpi.py`：`enable_dpi_awareness()`（Per-Monitor V2 → shcore(2) → SetProcessDPIAware 幂等回退）、`detect_dpi_scale()`（优先环境变量 `CUBEFETCH_DPI_SCALE`，其次 `GetDpiForSystem()/96`）。
- `theme.py` 新增 `SCALE` 与 `sp(px)`；窗口/控件固定尺寸统一按 SCALE 缩放；字体仍用 pt，由 `root.tk.call("tk","scaling", SCALE*96/72)` 处理。
- `app.py`：创建 Tk 前调用 `_enable_dpi_awareness()`（`--selftest` 与 `_fatal` 路径同样调用）；`create_app()` 内部再调用一次（幂等）。
- 本机实测 150%（144 DPI）：窗口 1470x990，布局无溢出/裁切；模拟 1.0/1.5 两档自测均通过。

### 15.3 进度口径（task-14 core + task-13 UI）
- `Progress` 增加 `stage/stage_done/stage_total`（见 §3）。
- `Installer.install()` 事件序列：准备 → 静态阶段（静态 100% 事件被抑制）→ 转场（一次性定最终 total/bytes_total）→ 资源阶段 → 解压/整理/写盘 → 「安装完成」100%。
- UI 百分比**按字节优先**（`bytes_done/bytes_total`）；「文件进度」显示整体 `done/total`；「本阶段 x / y」取自 `stage_done/stage_total`；`message` 不再携带分母。
- 每次进入进度页前 `reset()`；完成/失败保持 100%/中止态。


## 16. v1.1.2（2026-10-22，用户反馈 m02044）新增/变更

### 16.1 登录自动打开浏览器（ui/pages/login.py）
- 模块级钩子 `_browser_opener = webbrowser.open`（测试可替换）；拿到设备码后：自动复制到系统剪贴板 + 自动调用 `_browser_opener(verification_uri)` 一次（「重新获取验证码」拿到新码时同样再打开一次）。
- 打开失败只记日志，界面保留「打开浏览器」「复制代码」手动按钮兜底；提示文案为「验证码已复制，已自动打开浏览器」。
- 验证：`tools/smoke_login_autoopen.py`（真实网络设备码链路，替换 opener 后断言调用 URL 与剪贴板）。

### 16.2 液态玻璃 UI（纯 tkinter 绘制）
- 新增 `cubefetch/ui/glass.py`：`draw_glass_panel()`（分层圆角渐变 + 顶部高光/左上反光 + 1px 半透明冷紫描边）、`draw_vertical_gradient()`、`draw_corner_mask()`、`AuroraBackdrop`（4 光斑低饱和流动背景，页面隐藏即停，12 FPS）。
- `widgets.py`：`Card` 外层渐变环 + body 圆角遮罩；主/次/危险按钮渐变高光；`SegmentedControl` 玻璃胶囊；`ProgressBar` 渐变填充 + 柔光扫过；`Badge` 半透明白边；`VersionRow` 选中高光线。
- 页面路由：每页底层铺 `AuroraBackdrop`，切页 `pause()/resume()`；`pages/base.py` 统一处理。
- 性能预算：`create_app()` 玻璃开销约 282 ms（0.36 s 内含全部页面构建），首帧 136.9 ms；纯 Canvas 绘制，无图片资源。
