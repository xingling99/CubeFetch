# CubeFetch 打包脚本：生成单文件、自包含依赖的 Windows exe
# 用法: powershell -ExecutionPolicy Bypass -File build\build.ps1 [-SkipInstall] [-Clean]
param(
    [switch]$SkipInstall,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "== CubeFetch 打包 ==" -ForegroundColor Cyan
Write-Host "项目目录: $Root"

# 1) 选择 Python
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) {
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    if (Test-Path $candidate) { $Python = $candidate }
}
if (-not $Python) { throw "未找到 Python，请先安装 Python 3.13" }
Write-Host "Python: $Python"
& $Python -c "import sys; assert sys.version_info >= (3, 10), sys.version" 

# 2) 依赖
if (-not $SkipInstall) {
    Write-Host "安装运行依赖..." -ForegroundColor Yellow
    & $Python -m pip install --disable-pip-version-check -r requirements.txt
    Write-Host "安装打包工具 PyInstaller..." -ForegroundColor Yellow
    & $Python -m pip install --disable-pip-version-check pyinstaller
}

# 3) 图标（可选，用 Pillow 现画一个方块图标；没有 Pillow 就跳过）
$IconPath = Join-Path $Root 'build\cubefetch.ico'
& $Python -c @"
import sys
try:
    from PIL import Image, ImageDraw
except Exception:
    sys.exit(2)
sizes = [16, 24, 32, 48, 64, 128, 256]
base = 256
img = Image.new('RGBA', (base, base), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# 深色圆角底 + 主色立方体（简单等轴测方块）
d.rounded_rectangle([8, 8, base-8, base-8], radius=52, fill=(23, 23, 28, 255))
cx, cy, r = 128, 132, 74
top = [(cx, cy-r), (cx+r, cy-r//2), (cx, cy), (cx-r, cy-r//2)]
left = [(cx-r, cy-r//2), (cx, cy), (cx, cy+r), (cx-r, cy+r//2)]
right = [(cx, cy), (cx+r, cy-r//2), (cx+r, cy+r//2), (cx, cy+r)]
d.polygon(top, fill=(108, 92, 231, 255))
d.polygon(left, fill=(84, 71, 190, 255))
d.polygon(right, fill=(64, 54, 150, 255))
img.save(r'$IconPath', sizes=[(s, s) for s in sizes])
print('icon ok')
"@
if ($LASTEXITCODE -ne 0) {
    Write-Host "跳过图标生成（未安装 Pillow 或生成失败）" -ForegroundColor Yellow
    $IconPath = $null
} else {
    Write-Host "图标: $IconPath"
}

# 4) 清理
if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build\out, dist, CubeFetch.spec
}

# 4.5) 生成文件完整性清单（文件级防破解：清单必须与最终源码一致）
$GenIntegrity = Join-Path $Root 'build\gen_integrity.py'
if (Test-Path $GenIntegrity) {
    Write-Host "生成文件完整性清单..." -ForegroundColor Yellow
    & $Python -X utf8 $GenIntegrity
    if ($LASTEXITCODE -ne 0) { throw "生成完整性清单失败（退出码 $LASTEXITCODE）" }
} else {
    Write-Host "警告：未找到 build\gen_integrity.py，跳过文件完整性清单" -ForegroundColor Yellow
}

# 5) PyInstaller 打包
$pyArgs = @(
    '--noconfirm', '--clean',
    '--onefile',            # 单文件
    '--windowed',           # 无控制台窗口
    '--name', 'CubeFetch',
    '--distpath', 'dist',
    '--workpath', 'build\out',
    '--specpath', 'build',
    '--paths', $Root,
    '--hidden-import', 'cubefetch.ui.app_window',
    '--exclude-module', 'pytest',
    '--exclude-module', 'PIL',
    '--exclude-module', 'unittest',
    '--exclude-module', 'pydoc',
    '--exclude-module', 'doctest',
    '--exclude-module', 'xmlrpc'
)
if ($IconPath -and (Test-Path $IconPath)) { $pyArgs += @('--icon', $IconPath) }
$pyArgs += 'app.py'

Write-Host "PyInstaller 参数: $($pyArgs -join ' ')"
& $Python -m PyInstaller @pyArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（退出码 $LASTEXITCODE）" }

# 6) 结果
$Exe = Join-Path $Root 'dist\CubeFetch.exe'
if (-not (Test-Path $Exe)) { throw "未找到打包产物 $Exe" }
$SizeMB = [math]::Round((Get-Item $Exe).Length / 1MB, 2)
Write-Host ""
Write-Host "打包完成: $Exe （$SizeMB MB）" -ForegroundColor Green

# 7) 冒烟：启动 exe，3 秒后仍存活视为可启动，然后结束整个进程树
#    （PyInstaller onefile 的启动器会再拉起子进程，只杀父进程会留下后台实例）
Write-Host "冒烟测试：启动 exe..." -ForegroundColor Yellow
$proc = Start-Process -FilePath $Exe -PassThru
Start-Sleep -Seconds 4
$alive = -not $proc.HasExited
if ($alive) {
    Write-Host "exe 正常启动（进程 $($proc.Id) 存活）" -ForegroundColor Green
    & taskkill /PID $proc.Id /T /F 2>&1 | Out-Null
    Start-Sleep -Milliseconds 500
} else {
    Write-Host "警告：exe 启动后立即退出，退出码 $($proc.ExitCode)" -ForegroundColor Red
    Write-Host "请查看 %LOCALAPPDATA%\CubeFetch\logs\cubefetch.log"
    exit 1
}
