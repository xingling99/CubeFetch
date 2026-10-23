# CubeFetch 开发进度与验证记录（最终状态：源码完成，E2E 通过）

> 2026-10-20 立项，2026-10-21 完成全部源码与真机验证；2026-10-22 按用户反馈迭代到 **v1.1.1-beta** / **v1.1.2-beta**（DPI 缩放、按 versions 扫描检测、进度口径修正、登录自动开浏览器 + 液态玻璃）；2026-10-23 按用户反馈**整体重写 UI 为「深色现代极简」**并发布 **v1.2.0-beta**；同日按反馈「检测到目录就自动选择」发布 **v1.2.1-beta**（不再弹窗选目录）。本文件记录最终状态、实测数据与遗留风险。

## 一、需求覆盖（用户 11 条）

| # | 需求 | 实现位置 | 状态 |
|---|---|---|---|
| 1 | Xbox/Minecraft 接口认证；无正版不进下载界面 + 防破解/绕过 | `core/auth.py`（设备码 → MSA → XBL → XSTS → login_with_xbox → entitlements/profile）、`core/security.py`（Ticket/AuthGate/IntegrityGuard）、`core/account_store.py`（DPAPI+HMAC）、`ui/app_window.py`（未登录拦截 4 个下载页） | ✅ |
| 2 | 加载器可自定义 Fabric/Forge/NeoForge（另支持 Quilt） | `core/loader.py` + `core/installer_processors.py` | ✅ |
| 3 | 多线程下载游戏本体 | `core/http.py` ThreadingDownloader（默认 32 线程，断点续传、镜像回落） | ✅ |
| 4 | 自起名 | **CubeFetch（方块速递）** | ✅ |
| 5 | 正式版/预览版/远古版/愚人节版分类 | `core/manifest.py` `classify_version` + 9 个愚人节版 ID 白名单 | ✅ |
| 6 | 自动识别 PCL/HMCL 目录（检测到即自动选择版本数最多者，无需弹窗） | `core/detect.py` + `services.bootstrap` 自动写入默认根 + `ui/pages/settings.py` 自动检测直接选中 | ✅ |
| 7 | 下载版本自动版本隔离 | `core/isolation.py`（PCL `PCL/Setup.ini`、HMCL `instance-game-settings.json`/`hmclversion.cfg`），`services.install` 自动调用 | ✅ |
| 8 | 体积小流畅 | 单文件 exe **15 MB 级**（PyInstaller onefile），唯一运行时依赖 requests | ✅ |
| 9 | 记住账号 30 天，超期重验 | `account_store.needs_reverify(days=30)`、`auth.refresh` 超期抛 `AuthError(code='reverify')`、UI 强制重新登录 | ✅ |
| 10 | 简约风格 GUI | `ui/` 自绘深色主题（#17171c/#22222b/#6c5ce7/#e8e8ef），980×660 | ✅ |
| 11 | 打包 exe（依赖内嵌） | `build/build.ps1` → `dist\CubeFetch.exe` | ✅ |

## 二、最终验证数据（真实网络 / 真实窗口，2026-10-23，v1.2.1-beta）

- 全仓测试：`python -m pytest -q` → **418 passed, 71 subtests passed（~42s）**；`compileall cubefetch tools app.py build` = 0；
  UI 自测 `python -m cubefetch.ui._selftest` → 114 项通过 / 0 失败（1.0 与 1.5 两档缩放）。
- DPI：本机系统 150%（144 DPI），窗口 1620x1080，页面无溢出/裁切；`CUBEFETCH_DPI_SCALE` 可模拟 1.0/1.5。
- 现代深色极简 UI（v1.2.0）：`ui/theme.py`（色板/尺寸/sp 缩放）+ `ui/widgets.py`（渐变主按钮、描边次按钮、纯色圆角卡片、胶囊分段控件、行式列表、玻璃感进度条）+ `ui/app_window.py`（64px 顶栏 + 208px 侧栏胶囊导航 + 28px 状态栏）+ `ui/pages/*`（登录/下载/进度/完成/设置）+ `ui/dialogs.py`（自绘 Toplevel 弹窗）；
  create_app 54–89 ms、首帧约 104 ms（无玻璃/无光斑动画）；已删除 `ui/glass.py`。
- 登录自动打开浏览器：`tools/smoke_login_autoopen.py` 真实网络链路 PASS —— 点登录 → 真实设备码 `<DEVICE_CODE>` → 浏览器被打开 `https://www.microsoft.com/link` → 8 位验证码自动写入系统剪贴板 → 提示「验证码已复制，已自动打开浏览器」。
- 检测：真实机 `D:\pcl2\.minecraft（3 个版本）` 被识别为有效安装根，空目录只作弱候选；detect 用时约 30 ms。
- 原版 1.20.1：`tools/smoke_install.py` ✅ 20.9s，3577 个 assets 对象、64 jar、701.7 MB。
- 服务层 Fabric 0.19.5（1.20.1）：`tools/smoke_services.py` ✅ 17.0s，profile `1.20.1-fabric-0.19.5`，写入 `versions/<id>/PCL/Setup.ini`（`VersionArgumentIndieV2:True`）。
- 服务层 Forge 47.2.0（1.20.1）：✅ 148.3s，processors 补丁 + assets，146 jar、795.9 MB。
- 服务层 NeoForge 21.1.80（1.21.1，loader-dev 实测）：✅ 193.2s，`neoforge-21.1.80-client.jar` 5,561,321 B。
- GUI 真实后端：登录页 / 下载页（正式 103、预览 747、远古 61、愚人节 9，Fabric 255 个版本并自动推荐 0.19.5）/ 设置页 / 进度页（真实安装实拍 11%、16/75 文件、9.49/83.34 MB 口径一致）截图 `docs/shots/real_*.png`（150% 缩放下 1620x1080，现代深色极简）；未登录点击下载页会被拦回登录页。
- 设备码真实请求：获取到 `user_code`、`verification_uri=https://www.microsoft.com/link`、`expires_in=900`、`interval=5`；界面拿到设备码后自动复制到剪贴板并自动打开浏览器（`tools/smoke_login_autoopen.py` 真实网络 PASS）。
- 打包：`dist\CubeFetch.exe` 构建成功（13.87 MB / 14,545,793 B）；exe `--version` → `CubeFetch 1.2.1-beta`（exit 0）、`--selftest` → exit 0；
  真实窗口截图 `docs/shots/real_exe_login.png`（DPI 感知，150% 下 1620x1080，PrintWindow 抓取不被遮挡）；启动日志显示
  「程序文件完整性校验通过（冻结模式（打包））」。
- 发布包：`dist\CubeFetch-v1.2.1-beta-win64.zip`（exe+说明文档）与
  `dist\CubeFetch-v1.2.1-beta-source.zip`（112 个文件，含 build 脚本）；运行包在「全新 `%LOCALAPPDATA%`」下启动并生成默认配置（自动写入 `mc_root=D:\pcl2\.minecraft`，无弹窗）、`--version` 正常；源码包内 `pytest` 418 passed。
- 最终 SHA256（exe 固定；两个 zip 的 SHA256 见交付消息，因文档更新会重打包，zip 哈希不写进包内文档）：
  exe `DFF2E11DEB74FEB5A557059453DA93FB3E77D226FCF9C680B55E8A03F869E791`
- 真机 GUI 冒烟时发现并修复一个真实 bug：Windows 某些滚轮事件的 `event.num` 是字符串 `"??"`，
  `cubefetch/ui/widgets.py` 的 `_on_wheel` 直接 `int()` 会抛 `ValueError`（表现为刷屏的
  "Exception in Tkinter callback"）。已加 try/except 并新增回归测试 `tests/test_ui_wheel.py`（3 例）。

## 三、关键技术决策（踩坑后确定）

1. **Forge/NeoForge 不用官方 `--installClient`**：其 processors 内部硬编码
   `https://launchermeta.mojang.com/mc/game/version_manifest.json`，本机下载 mojmap 时
   `java.net.SocketException: Connection reset`，安装必挂；`--mirror` 不会重写该 URL
   （反编译确认 mirror 只用于界面展示）。→ 改为 `installer_processors.py` 自己跑：
   解包 installer jar → 预置 libraries（BMCLAPI 优先，sha1 命中跳过）→ data 占位符替换 →
   `DOWNLOAD_MOJMAPS` 用原版 `downloads.client_mappings`（经 HttpSession 镜像）替代 →
   其余 client processors `java -cp "classpath;jar" <MANIFEST Main-Class> <args>` →
   写 `versions/<id>/<id>.json` → `Installer.install` 补 assets。
   `LoaderService(use_official_installer=True)` 仅在 `mirror=="official"` 时自动回退官方路径。
2. **BMCLAPI 镜像规则**（`core/http.py` MIRROR_RULES）：
   - `piston-meta.mojang.com/mc/game/` → `bmclapi2.bangbang93.com/mc/game/`（**必须保留完整 path**，根路径 404）
   - `libraries.minecraft.net/`、`maven.neoforged.net/releases/` → `bmclapi2.bangbang93.com/maven/`
   - launchermeta/piston-data/fabric-meta 等各自的 `/mc/`、`/fabric-meta/` 前缀
   - BMCLAPI 对默认 python-requests UA 返回 403，必须带 UA（已默认 `CubeFetch/1.0`）
3. **现代版本 natives 不解压**（LWJGL jar 内 dll 由 SharedLibraryLoader 自行加载，官方启动器行为一致）；
   仅旧式 `natives` + `extract` 解压到 `versions/<id>/<id>-natives/`。
4. 认证端点用 `login.live.com` 旧版设备码端点（`login.microsoftonline.com` 对该 client_id 返回 AADSTS700016）；
   轮询参数名必须是 `device_code`；MC token 24h 不可 refresh，需重放 XBL→XSTS→login_with_xbox。
5. 账号存储：DPAPI（CryptProtectData，UI_FORBIDDEN）+ HMAC-SHA256 信封，密钥绑定 MachineGuid；
   非 Windows 明文+HMAC 回退（本项目只发布 Windows exe）。
6. 目录自动选择（v1.2.1 起）：`services.bootstrap` 在 `config.mc_root` 为空时直接写入 `detection.default_root`（版本数最多的有效安装根）并保存；启动不再弹模态目录选择（`AppWindow.startup` 已移除 `_maybe_choose_root`）；设置页「自动检测」同样直接选中并调用 `services.choose_root` 保存。`show_root_dialog`/`_RootChoiceDialog` 保留供手动选择/测试。

## 四、已知风险 / 后续可改进

1. NeoForge 首次安装较慢（元数据 + processors ~2-3 分钟，个别库重试 ~90s），已加进度提示与重试。
2. `_find_existing_profile` 幂等匹配在同游戏多 loader 场景可能误判（常规场景已覆盖测试）。
3. `instances.install` 的 assets 全量下载对 Forge/NeoForge 走的是与原版相同的成熟路径
   （vanilla 已验证 701 MB，processors 路径只验证到写版本 JSON，`--full` 可补跑）。
4. HMCL 版本隔离写入基于文档调研（HMCL 本次机器未安装），未真机验证；PCL 已生效。
5. 认证调研的成功响应字段来自三个开源实现解析器（未用真实账号做端到端登录验证）。
6. 愚人节版/远古版安装未单独 E2E；版本清单分类、规则求值由离线 fixture 覆盖。

## 五、开发期工具（可复跑）

- `tools/smoke_install.py`：真实网络安装原版（`--root --game [--mirror --threads]`）。
- `tools/smoke_services.py`：服务层全链路（门禁 + 安装 + 隔离），支持 `--kind vanilla|fabric|quilt|forge|neoforge [--loader 版本]`。
- `tools/smoke_gui.py`：真实 exe/进程窗口截图（DPI aware）。
- `tools/smoke_gui_real.py`：真实 AppServices + 伪造账号驱动 GUI 并截图。
- `tools/inspect_installer.py`、`tools/neo_preseed_probe.py`、`tools/validate_processors_install.py`：安装器结构与 processors 路径验证。

## 六、文档

- `docs/CONTRACTS.md`：模块接口契约（实现依据）。
- `docs/research/auth.md`：认证链调研（49 KB，含 8 个 XErr 错误码语义）。
- `docs/research/ecosystem.md`：PCL/HMCL 配置、Fabric/Quilt/Forge/NeoForge 安装方式、BMCLAPI 实测。
- `docs/research/loader-notes.md`：加载器实现笔记（含 C6 processors 路径）。
- `docs/shots/`：GUI 截图（README 与验收用）。

## 七、v1.0.1 变更（2026-10-21，用户反馈 m01243）

用户原话：「模组加载器和下载版本整合到一个界面呗，方便用户下载，然后设置界面的启动器检测显示就直接检测到什么启动器，别把hmcl和pcl启动器显示出来，有点难看，然后下载源尽量用镜像源，打开浏览器登录界面自动复制代码，方便用户」

| # | 变更 | 位置 | 验证 |
|---|---|---|---|
| 1 | 「版本」+「加载器」两页合并为单个「下载」页；导航 6→5 项（登录/下载/进度/完成/设置）；右侧加载器单选 + 加载器版本下拉（自动推荐）+ 开始下载 | `ui/pages/download.py`（新增，约 1030 行）、删除 `ui/pages/{versions,loaders}.py`、`ui/app_window.py` | selftest 64 项；真实网络截图 `docs/shots/real_download.png`（103/747/61/9 分类、Fabric 255 版本推荐 0.19.5） |
| 2 | 设置页检测区域只显示去重后的游戏目录路径，不再出现 PCL2/HMCL 品牌名、「未检测到」空行 | `ui/pages/settings.py` | selftest 断言无「PCL2 启动器」「HMCL 启动器」文本；截图 `real_settings.png` |
| 3 | 下载源默认优先镜像：`HttpSession(mirror="auto")` 改为先 BMCLAPI、失败回落官方；新增 `"mirror"` 别名；删除「官方成功后记住官方」的反转记忆 | `core/http.py`（install-dev） | 新增/调整测试后 298 passed；真实网络 manifest/version json/asset index/资源对象/gson/23 MB client.jar 全部 MIRROR 200，logging config MIRROR 404→OFFICIAL 200 回落且后续仍镜像优先 |
| 4 | 登录页拿到设备码后自动写系统剪贴板并提示「验证码已复制」；新增「重新获取验证码」入口（新码同样复制） | `ui/pages/login.py` | selftest 断言剪贴板内容 == 设备码、提示文案存在 |

配套：`docs/CONTRACTS.md` 镜像语义改为「auto = 先镜像后官方」；版本号 1.0.0 → 1.0.1（`cubefetch/__init__.py`、`config.py`、`build/make_release.py`）；
`README.md`、`release/使用说明.txt` 同步更新；发布包改名 `CubeFetch-v1.0.1-*.zip`。

## 八、v1.1.0-beta 变更（2026-10-21，用户反馈 m01405）

用户 7 条需求与实现：

| # | 需求 | 实现 | 验证 |
|---|---|---|---|
| 1 | 文件级防破解 | `build/gen_integrity.py` 生成 `cubefetch/integrity_manifest.py`（33 文件 sha256 + 33 模块规范化 code hash）；`core/integrity.py` 源码/冻结双模式校验；`security.set_integrity_problems()` 让门禁抛 `AuthError(code="integrity")`；`app.py` 启动校验并弹窗/状态栏警告 | 源码模式 33/33 匹配；篡改/缺失/新增文件均检出；打包后日志「程序文件完整性校验通过（冻结模式（打包））」 |
| 2 | 打开自动拉取并刷新版本列表 | `ui/pages/download.py` 进程级首次进入强制 `versions_categories(refresh=True)`，状态栏显示「已加载 920 个版本」 | 真实网络 GUI 冒烟：进入下载页 4s 内显示 103/747/61/9 分类 |
| 3 | 无合适 Java 时询问并一起下载 | `core/java_runtime.py`（Mojang java-runtime 索引，复用 HttpSession/ThreadedDownloader）；`services.java_status()/download_java()`；UI 弹窗（约 95MB）→ 进度页先下 Java 再安装，Java 安装到 `.minecraft\runtime` 并记入配置 | 真实下载 `java-runtime-delta` 21.0.7：402 文件 / 94.5 MiB / 234.5s，`java -version` exit 0；`services.java_status("1.20.4")` 返回 gamma + 本机 JDK21 |
| 4 | 版本重名时让用户输入版本名 | `core/version_rename.py`（校验/移动/改 JSON id/同步 inheritsFrom 引用）；UI 用 `services.existing_version()` 检测后 `askstring` 输入并路由到安装 | 真实目录重命名实测（JSON id 改写、旧目录清理、非法名拒绝、同名幂等） |
| 5 | 所有提醒加音效 | `ui/sound.py`（winsound 异步 + MessageBeep 回退 + 同 kind 0.3s 节流）；`ui/dialogs.py` 统一弹窗；NoticeBar/状态栏/完成页接入 | selftest 断言四类音效与节流；真机 `play("info") -> True` |
| 6 | 下载中关闭窗口弹确认 | `app_window.request_close()` + `create_app` 绑定 `WM_DELETE_WINDOW`；有任务时 askyesno | selftest 覆盖「否→不关 / 是→关」；无任务时 `request_close()` 正常退出 |
| 7 | 版本号带 beta | `cubefetch/__init__.py`、`config.py`、`build/make_release.py` 全部 1.0.1 → **1.1.0-beta** | exe `--version` 输出 `CubeFetch 1.1.0-beta`；窗口标题/关于信息一致 |

配套改动：
- 删除死代码 `cubefetch/i18n.py`（从未被 import，PyInstaller 裁包后会被完整性校验误判为盲区）；`docs/CONTRACTS.md`、`README.md` 同步移除；
- 打包流程：`build/build.ps1` 在 PyInstaller 前自动跑 `gen_integrity.py`；
- 测试：`tests/test_java_runtime.py`（42）、`test_integrity.py`（26）、`test_version_rename.py`（35）、`test_ui_wheel.py`（3）；全仓 **401 passed, 71 subtests**；UI 自测 **91 项**；
- 发布包：`CubeFetch-v1.1.0-beta-win64.zip`、`CubeFetch-v1.1.0-beta-source.zip`（`build/make_release.py`）。

## 九、v1.1.1-beta 变更（2026-10-22，用户反馈 m01768）

用户原话：「有几个点要修改。1.检测minecraft改成检测启动器的D:\pcl2\.minecraft\versions的这类文件。2.窗口化的dpi有显示问题，缩放有问题。3.任务进度显示的不够准确」

| # | 问题 | 修复 | 验证 |
|---|---|---|---|
| 1 | 「自动检测」只依赖启动器配置里的路径，配置丢失/换目录就检测不到 | `core/detect.py` 新增**有效安装根扫描**：判据 `<root>\versions\<id>\<id>.json` ≥1；候选 = 配置根 + `%APPDATA%\.minecraft` + 启动器目录下 `.minecraft` + 桌面/下载/文档/固定盘符一级目录；`DetectionResult` 新增 `root_versions: list[(Path,int)]` 与 `root_version_counts`；`default_root` 取版本数最多者；空 `.minecraft` 降为弱候选 | 真实机识别 `D:\pcl2\.minecraft（3 个版本）`；`tests/test_detect.py` 25 例（scan_roots 注入、排序、空目录、单/双启动器默认根）；detect ~30 ms |
| 2 | 未声明 DPI 感知，125%/150% 缩放下整窗位图放大：字体模糊、布局错位 | 新增 `ui/dpi.py`（Per-Monitor V2 → shcore(2) → SetProcessDPIAware 幂等回退）；`theme.SCALE` + `sp(px)` 统一缩放窗口/控件尺寸；`app.py` 与 `create_app()` 创建 Tk 前启用感知；字体用 pt 由 Tk scaling 处理；`tests/test_ui_dpi.py` 覆盖缩放常量与 sp() | 本机 150%（144 DPI）窗口 1470x990，登录/下载/进度/完成/设置页人工复核无溢出/裁切；`CUBEFETCH_DPI_SCALE=1.0/1.5` 自测 108 项全绿，截图 `docs/shots/scale100/`、`docs/shots/dpi150/` |
| 3 | 进度显示自相矛盾：阶段文案 5056/5057、文件进度 5154/5155、百分比 00.0%，且静态→资源阶段口径切换导致跳变 | `Progress` 增加 `stage/stage_done/stage_total`：`done/total` 永远是整体文件数、`bytes_*` 永远整体字节数、`message` 不再含分母；`Installer.install()` 静态阶段 100% 事件抑制、进资源阶段前一次性定最终 `total/bytes_total`、结束强制 100%；UI 百分比按字节优先、另起「本阶段 x/y」行、`reset()` 清状态 | `tests/test_install.py::ProgressInvariantTest`（现代+legacy virtual 离线全安装，断言单调/边界/message 无数字/最终 100%）；真实安装进度页实拍 `docs/shots/real_progress_1.png`、`real_progress_2.png` |

配套：
- 版本号 1.1.0-beta → **1.1.1-beta**（`cubefetch/__init__.py`、`config.py`、`build/make_release.py`）；`docs/CONTRACTS.md` 更新 §3 Progress、§6 DetectionResult 并新增 §15；
- 测试基线：全仓 **416 passed + 71 subtests**；UI 自测 **108 项**；`compileall` = 0；
- 发布包改名 `CubeFetch-v1.1.1-beta-*.zip`；桌面快捷方式 `CubeFetch 方块速递.lnk` 指向同一 `dist\CubeFetch.exe`，重打包无需重建。

## 十、v1.1.2-beta 变更（2026-10-22，用户反馈 m02044）

用户原话：「点击登录按钮自动打开浏览器，界面学习一下网上很火的液态玻璃元素，可以加一点液态玻璃的元素」

| # | 需求 | 实现 | 验证 |
|---|---|---|---|
| 1 | 点击登录自动打开浏览器 | `ui/pages/login.py` 增加模块级 `_browser_opener`（默认 `webbrowser.open`）；拿到设备码的事件里自动复制验证码 + 自动打开一次（「重新获取验证码」拿新码也同样打开）；打开失败仅记日志，保留「打开浏览器 / 复制代码」手动按钮兜底；提示文案同步为「点击后会自动打开浏览器并复制验证码」 | `tools/smoke_login_autoopen.py` 真实网络 PASS：写入剪贴板 `256A44QG`、调用 `https://www.microsoft.com/link` 一次、提示含「自动打开浏览器」；`_selftest.py` 断言打开次数/URL/失败静默 |
| 2 | 液态玻璃 UI 元素 | 新增 `ui/glass.py`：分层圆角渐变玻璃面板、顶部高光/左上反光、1px 半透明冷紫描边、`AuroraBackdrop` 4 光斑低饱和流动背景（页面隐藏即停、12 FPS）；`widgets.py` 玻璃化主/次/危险按钮、`Card`（外层渐变环 + body 圆角遮罩）、`SegmentedControl`、`ProgressBar`（渐变填充 + 柔光扫过）、`Badge`；页面底层铺光斑、切页 pause/resume | 截图 `docs/shots/{login,download,progress,done,settings}.png` 与 `docs/shots/dpi150/` 同名一套（1.0/1.5 两档人工复核：无直角内框/无接缝）；性能 create_app 361.8 ms（玻璃开销约 282 ms）、首帧 136.9 ms；`_selftest` 116 项 0 失败 |

配套（v1.1.2-beta）：
- 版本号 1.1.1-beta → **1.1.2-beta**（`cubefetch/__init__.py`、`config.py`、`build/make_release.py`）；
- 打包前重跑 `build/gen_integrity.py`（FILES=35、CODE=35，源码变更后必须重生成，否则 `tests/test_integrity.py` 会 skip 提示清单过期）；
- 测试基线：全仓 **416 passed + 71 subtests**；UI 自测 **116 项**；`compileall` = 0；
- 发布包改名 `CubeFetch-v1.1.2-beta-*.zip`；桌面快捷方式指向同一 `dist\CubeFetch.exe`，重打包无需重建。

## 十一、v1.2.0-beta 变更（2026-10-23，用户反馈 m02405：UI 重写）

用户原话：「重新写ui，这个ui太复古了，改成现代化的」。用户选择方向：**深色现代极简**（Linear / Raycast 风；不是玻璃 2.0）。

| 项 | 实现 | 验证 |
|---|---|---|
| 设计系统 | 重写 `ui/theme.py`：背景 `#0e0e12`、浮层 `#17171d`/`#1d1d25`、描边 `#2a2a35`、文字三级灰、强调渐变 `#7c6cf0→#58a6ff`、成功/警告/危险色；字号层级 + `sp()` 尺寸缩放；窗口 1080×720（min 960×640），DPI 1.0/1.5 均无溢出 | `CUBEFETCH_DPI_SCALE=1.0/1.5` selftest 114 项 0 失败；截图 `docs/shots/` 与 `docs/shots/dpi150/`（1620×1080） |
| 布局 | 64px 顶栏（logo/品牌/版本 chip + 右侧账号胶囊）+ 208px 侧栏 Canvas 自绘胶囊导航（高 40、圆角 10、选中强调 14% 底）+ 28px 状态栏 | 截图人工复核：无厚灰边框/无画框感，层级清晰 |
| 控件 | 主按钮渐变圆角 10、次按钮描边、分类胶囊 Tab、行高 44 列表（hover 白 5%、选中强调 12% 底 + 左侧 2px 强调条）、2 列 72px 加载器选择卡、渐变进度条（6px，保留 shimmer） | selftest 覆盖交互状态；真实 GUI 截图 `real_download.png`（150% 下 1620×1080） |
| 页面 | 登录（居中 460 卡片 + 极淡紫蓝光晕）；下载（左版本列表/右加载器卡 + 底部通栏动作条）；进度（48px 大号百分比 + 6px 渐变条 + 统计内嵌块 + 可折叠日志）；完成（居中状态卡 + 元信息两列）；设置（4 组卡片 + 标签 120 + 底部保存条） | `python app.py --selftest` PASS；`tools/smoke_gui_real.py` 真实后端 PASS（下载/设置/进度页实拍） |
| 弹窗 | `ui/dialogs.py` 改为自绘现代 Toplevel（圆角卡片、标题/正文/按钮行、Esc/Enter、居中 grab_set），保留 `ask_yes_no/ask_string/show_info/show_warning/show_error` API；关闭确认、Java 询问、版本重命名、根目录选择全部走它 | selftest 覆盖确认/输入/关闭分支；截图 `docs/shots/dialog.png` |
| 清理 | 删除 `ui/glass.py` 与全部玻璃/光斑动画（保留登录页静态紫蓝光晕）；新增 `tools/smoke_gui.py` PrintWindow 抓取（窗口被遮挡也能截图） | `compileall cubefetch\ui` = 0 |

配套（v1.2.0-beta）：
- 版本号 1.1.2-beta → **1.2.0-beta**（`cubefetch/__init__.py`、`config.py`、`build/make_release.py`）；
- 打包前重跑 `build/gen_integrity.py`（FILES=34、CODE=34，UI 重写删除了 glass.py 并新增 dpi 等模块后数量变化）；
- 测试基线：全仓 **418 passed + 71 subtests**；UI 自测 **114 项**；`compileall` = 0；
- 发布包改名 `CubeFetch-v1.2.0-beta-*.zip`；桌面快捷方式指向同一 `dist\CubeFetch.exe`，重打包无需重建；
- 功能回归清单（页面路由/未登录拦截/登录自动开浏览器+复制验证码/Java 询问/版本重命名/取消/失败态/音效/设置目录显示/关闭确认）在 `_selftest.py` 中全部保留通过。

## 十二、v1.2.1-beta 变更（2026-10-23，用户反馈 m02559：下载目录检测到了就自动选择）

用户原话：「下载目录检测到了就自动选择啊」。

| 项 | 修改 | 验证 |
|---|---|---|
| 启动自动选目录 | `cubefetch/services.py` `bootstrap()`：只要 `config.mc_root` 为空且检测到 `default_root`，就直接写入并 `save()`（原逻辑仅 `not needs_choice` 才写，双启动器场景会留给弹窗） | 全新 `%LOCALAPPDATA%` 下 exe 首次启动：`config.json` 自动写入 `"mc_root": "D:\\pcl2\\.minecraft"`，无弹窗 |
| 移除启动弹窗 | `cubefetch/ui/app_window.py`：`startup()` 删除 `after(150, _maybe_choose_root)`，删除 `_maybe_choose_root`；保留 `show_root_dialog()` 与 `_RootChoiceDialog` 供手动/测试 | 启动路由仍正常（有账号→下载页，无账号→登录页）；selftest 中手动弹窗用例仍通过 |
| 设置页自动检测 | `cubefetch/ui/pages/settings.py` `_on_detected()`：无条件 `_root_var.set(default_root)` + `services.choose_root()` 保存 + 提示「已自动选择游戏目录：<路径>（N 个版本）」 | 真实机检测 `D:\pcl2\.minecraft（3 个版本）` 自动选中并保存 |

配套（v1.2.1-beta）：
- 版本号 1.2.0-beta → **1.2.1-beta**（`cubefetch/__init__.py`、`config.py`、`build/make_release.py`）；
- 打包前重跑 `build/gen_integrity.py`（FILES=34、CODE=34）；
- 测试基线：全仓 **418 passed + 71 subtests**；UI 自测 **114 项**；`app.py --selftest` PASS；`compileall` = 0；
- 发布包改名 `CubeFetch-v1.2.1-beta-*.zip`；桌面快捷方式指向同一 `dist\CubeFetch.exe`，重打包无需重建。
