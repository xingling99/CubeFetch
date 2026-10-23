# CubeFetch（方块速递）

一个轻量、简约的 **Minecraft Java 版下载器**，Windows 桌面应用。

> **关于本项目**：CubeFetch 由 **大肥鱼** 开发，是一个纯兴趣驱动的业余项目，目的很简单——让玩家能更省心、更安全地下载 Minecraft Java 版游戏本体和模组加载器，不用再为找版本、配 Java、装加载器折腾。游戏文件只从 Mojang 官方 CDN（或其公开镜像 BMCLAPI）获取，并且只有通过 Microsoft 正版所有权校验后才允许下载。如果这个工具帮到了你，欢迎点个 ⭐ Star；遇到问题或想要新功能，欢迎提 Issue。

> **关于作者**：作者还是一名高三学生，GitHub 也是刚上手，很多功能都还不熟悉。如果各位 GitHub 大佬发现项目里有错误、写得不规范的地方，或者有更好的实现思路，欢迎直接指出，我会认真修改；也欢迎提 Issue / PR，感谢指教 🙏

- 使用 **Microsoft 正版账号** 登录（Xbox Live → XSTS → Minecraft Services 完整认证链），没有正版账号无法进入下载界面；
- **一个「下载」页完成全部选择**：正式版 / 预览版 / 远古版 / 愚人节版分类 + 搜索 + 模组加载器（Fabric / Quilt / Forge / NeoForge / 原版），选好直接开始下载；
- 多线程下载游戏本体与资源文件，**默认优先走 BMCLAPI 镜像**，镜像不可用时自动回落官方源；
- 点击登录后**自动打开浏览器**，并把设备验证码自动复制到剪贴板，直接粘贴即可；
- 自动识别已安装的 **PCL2 / HMCL** 的 `.minecraft` 目录：除了读启动器配置，还会按 `<目录>\versions\<id>\<id>.json` 主动扫描已安装的游戏目录，**检测到后直接自动选中版本数最多的目录**（不再弹窗，可在设置里点「自动检测 / 浏览…」更换）；设置页只显示检测到的目录路径与各自版本数；
- 下载完成后自动写入**版本隔离**配置（PCL2 / HMCL），不破坏已有配置；
- 账号安全保存在本机（Windows DPAPI 加密），默认 **30 天** 有效，超期强制重新验证；
- **文件级完整性校验**：启动时校验程序文件（源码模式逐文件 sha256；打包后逐模块 code hash），被篡改会提示并禁止下载（提高破解门槛，非绝对防护）；
- 每次打开「下载」页自动拉取最新版本清单并刷新列表；
- 下载前检测 Java：没有合适 Java 时弹窗询问是否一并下载对应的**官方 Java 运行时**（默认装到 `.minecraft\runtime`）；
- 若目标版本已存在（版本重命名场景），弹窗让你自己输入新的版本名；
- 所有提示都带**提醒音效**；有任务进行中关闭窗口会先弹窗确认；
- **DPI 感知**：125% / 150% 缩放屏下按系统 DPI 放大界面，字体与布局清晰不错位；
- **现代深色极简 UI**（v1.2.0 起重写）：近黑背景 + 大圆角浮层卡片 + 1px 细边线、紫青渐变强调色、顶栏/侧栏胶囊导航、胶囊分类标签、大号进度数字、自绘现代弹窗；纯 tkinter Canvas 绘制、无图片资源；
- 纯 tkinter 实现，**唯一运行时第三方依赖是 requests**。

## 下载安装（普通用户）

1. 打开本仓库右侧的 **[Releases](https://github.com/xingling99/CubeFetch/releases/latest)** 页面；
2. 下载最新的 **`CubeFetch-*-win64.zip`**；
3. 解压到任意文件夹（建议单独放一个目录，方便以后更新）；
4. 双击 **`CubeFetch.exe`** 即可运行 —— 单文件，运行库与依赖（含 Python）已全部打包，**无需安装 Python / Java**；
5. 首次运行如遇 Windows SmartScreen「未知发布者」提示，点 **「更多信息」→「仍要运行」** 即可（exe 未做代码签名，属正常提示）。

> - 普通玩家只需要下载 `*-win64.zip`；`*-source.zip` 是源码包，供开发者/审计使用，无需下载。
> - 账号与设置保存在 `%LOCALAPPDATA%\CubeFetch\`（账号使用 Windows DPAPI 加密），卸载时删除解压目录与该文件夹即可。
> - 本工具需要 **Microsoft 正版账号** 登录，不提供离线登录或绕过正版验证。

## 系统要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（x64） |
| 账号 | Microsoft 账号，且已在 Xbox / Minecraft 中完成设置、拥有 Java 版 |
| 网络 | 能访问 `*.mojang.com`、`*.xboxlive.com`、`*.minecraftservices.com`（国内可走 BMCLAPI 镜像加速文件下载） |
| Java | 安装 Forge / NeoForge 时需要本机 Java（自动探测；也可在设置中手动指定）；没有合适 Java 时可在下载时一并下载官方 Java 运行时 |

> 本工具不提供离线登录、不内置任何破解/绕过正版验证的功能，游戏文件仅从 Mojang 官方 CDN（或其公开镜像 BMCLAPI）下载，且仅在账号通过 Minecraft 所有权校验后才允许下载。

## 运行（源码方式）

```powershell
# 安装依赖（仅 requests）
python -m pip install -r requirements.txt

# 启动
python app.py
```

程序数据保存在 `%LOCALAPPDATA%\CubeFetch\`：

- `config.json`：设置（下载源、线程数、.minecraft 目录、登录有效期等）
- `account.dat`：DPAPI 加密的账号凭据
- `cache/`：版本清单等缓存
- `logs/cubefetch.log`：运行日志

## 打包为单文件 exe

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

产物：`dist\CubeFetch.exe`（运行库与依赖全部打包进单个 exe，无需安装 Python）。

发布包（含运行包 + 源码包，中文文件名已按 UTF-8 写入 zip）：

```powershell
python -X utf8 build\make_release.py
```

- `dist\CubeFetch-v1.2.1-beta-win64.zip`：exe + README + 使用说明 + 进度文档
- `dist\CubeFetch-v1.2.1-beta-source.zip`：完整源码（不含 build/dist 产物）

> 打包前 `build\build.ps1` 会自动运行 `build\gen_integrity.py` 重生成文件完整性清单，
> 因此每次改完源码重新打包都会得到与新代码匹配的校验清单。

## 项目结构

```
app.py                  入口
cubefetch/
  config.py             配置与路径
  services.py           AppServices 门面（UI ↔ 核心逻辑）
  core/
    http.py             会话、镜像重写、多线程下载
    manifest.py         版本清单与四分类
    install.py          版本安装（规则求值/natives/assets/legacy）
    auth.py             Microsoft → Xbox → Minecraft 认证
    account_store.py    DPAPI 加密账号存储
    security.py         门禁票据 / 完整性检查
    integrity.py        文件级完整性校验（源码/冻结双模式）
    java_runtime.py     官方 Java 运行时下载（Mojang java-runtime）
    version_rename.py   已安装版本重命名
    detect.py           PCL2 / HMCL 检测
    isolation.py        版本隔离配置写入
    loader.py           Fabric / Quilt / Forge / NeoForge 安装
    installer_processors.py  Forge / NeoForge 安装器的 processors 自执行路径
  ui/                   现代深色极简 tkinter 界面（theme/widgets/pages、dpi.py DPI 缩放、sound.py 提醒音效、dialogs.py 自绘弹窗）
tests/                  单元测试
docs/                   契约与调研文档
build/                  打包脚本
```

## 开发者

```powershell
# 单元测试
python -m pytest -q
# 界面离线自测（不联网）
python -m cubefetch.ui._selftest
```

模块接口以 `docs/CONTRACTS.md` 为准；开发期实测记录见 `docs/PROGRESS.md` 与 `docs/research/`。

## 免责声明

本项目为第三方开源工具，与 Mojang Studios、Microsoft、网易、PCL2 或 HMCL 官方无关。
请遵守 Minecraft 最终用户许可协议（EULA）与当地法律法规；因使用本工具产生的任何后果由使用者自行承担。
