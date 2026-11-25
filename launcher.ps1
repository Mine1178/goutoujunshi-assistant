$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "[1/3] Looking for Python with tkinter..."

$candidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
    "C:\Python313\python.exe",
    "C:\Python312\python.exe",
    "C:\Python311\python.exe",
    "C:\Python310\python.exe"
)

$py = $null
foreach ($c in $candidates) {
    if (-not (Test-Path -LiteralPath $c)) { continue }
    & $c -c "import tkinter" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
}

if (-not $py) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) { $py = $found.Source }
}

if (-not $py) {
    Write-Host "[ERROR] No usable Python found (tkinter required)."
    Write-Host "Install Python 3.10+ with tcl/tk checked, then run this again."
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host ("       using " + $py)

Write-Host "[2/3] Checking dependencies..."
& $py -c "import tkinter, requests, keyboard, pyperclip, win32gui, PIL" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "       installing, may take a minute..."
    & $py -m pip install --disable-pip-version-check -r (Join-Path $root "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] pip install failed. Check your network."
        Read-Host "Press Enter to exit"
        exit 1
    }
}

Write-Host "[3/4] Preparing skill and icons..."
& $py (Join-Path $root "tools\bootstrap.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Could not fetch skill or create assets. Check network/git and retry."
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "[4/4] Starting..."
$pyw = $py -replace 'python\.exe$', 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pyw)) { $pyw = $py }
Start-Process -FilePath $pyw -ArgumentList "main.py" -WorkingDirectory $root
exit 0

