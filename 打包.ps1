# 狗头军师助手 一键打包（Windows）
# 产物：
#   dist\GoutouJunshi\GoutouJunshi.exe
#   dist\GoutouJunshi_Setup_v2.5.exe
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Py = $null
$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd) { $Py = $cmd.Source }
if (-not $Py) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
    )
    foreach ($c in $candidates) { if (Test-Path -LiteralPath $c) { $Py = $c; break } }
}
if (-not $Py) { throw "找不到 Python，请先安装 Python 3.10+ 并加入 PATH" }

Write-Host "[1/4] 语法检查与资源准备"
& $Py -m py_compile main.py ui.py contacts.py store.py version.py skill_engine.py system_integration.py llm_client.py wechat_bridge.py wechat_import.py
if ($LASTEXITCODE -ne 0) { throw "py_compile 失败" }
if (-not (Test-Path "skill\goutoujunshi\SKILL.md") -or -not (Test-Path "assets\icon.ico")) {
    & $Py tools\bootstrap.py
    if ($LASTEXITCODE -ne 0) { throw "bootstrap 失败" }
}

Write-Host "[2/4] PyInstaller 目录版"
if (Test-Path "build") { Remove-Item "build" -Recurse -Force }
if (Test-Path "dist") { Remove-Item "dist" -Recurse -Force }
& $Py -m PyInstaller --noconfirm --clean --onedir --windowed `
  --name GoutouJunshi --icon "assets\icon.ico" `
  --hidden-import pystray._win32 --hidden-import keyboard._winkeyboard `
  --hidden-import PIL.ImageTk --hidden-import PIL.ImageOps `
  --collect-submodules pystray `
  --exclude-module numpy --exclude-module pandas --exclude-module matplotlib `
  --exclude-module scipy --exclude-module PyQt5 --exclude-module PySide6 `
  --exclude-module pytest main.py 2>&1 | Tee-Object -FilePath "build_pyinstaller.log" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，见 build_pyinstaller.log" }

Write-Host "[3/4] 放入完整技能与必要图标"
$Dist = Join-Path $Root "dist\GoutouJunshi"
New-Item -ItemType Directory -Force -Path (Join-Path $Dist "skill\goutoujunshi") | Out-Null
robocopy "skill\goutoujunshi" (Join-Path $Dist "skill\goutoujunshi") /E /XD .git .github __pycache__ /XF *.pyc | Out-Null
if ($LASTEXITCODE -ge 8) { throw "复制技能失败" }
New-Item -ItemType Directory -Force -Path (Join-Path $Dist "assets") | Out-Null
Copy-Item "assets\icon.ico" (Join-Path $Dist "assets\icon.ico") -Force
Copy-Item "assets\logo_256.png" (Join-Path $Dist "assets\logo_256.png") -Force

Write-Host "[4/4] 生成安装程序"
$ISCC = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path -LiteralPath $ISCC)) { throw "未找到 Inno Setup 6：$ISCC" }
& $ISCC (Join-Path $Root "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败" }

Write-Host "完成："
Write-Host "  目录版：$Dist"
Write-Host "  安装包：$Root\dist\GoutouJunshi_Setup_v2.5.exe"
