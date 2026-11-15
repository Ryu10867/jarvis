# ============================================================================
#  J.A.R.V.I.S. installer for Windows 10/11
#  Run:  install.bat   (or:  powershell -ExecutionPolicy Bypass -File install.ps1)
#
#  Program  ->  %LOCALAPPDATA%\Programs\Jarvis   (its own Python environment)
#  Settings, models, logs  ->  %LOCALAPPDATA%\Jarvis
#  Adds the `jarvis` command to your PATH and a "Jarvis" shortcut on the Desktop.
# ============================================================================
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$app  = Join-Path $env:LOCALAPPDATA 'Programs\Jarvis'
$venv = Join-Path $app 'venv'
$bin  = Join-Path $app 'bin'

function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }

Say "`n  J.A.R.V.I.S. installer`n"

# --- 1. the package itself
$wheel = Get-ChildItem $here -Filter 'jarvis_assistant-*.whl' | Sort-Object Name | Select-Object -Last 1
if (-not $wheel) { throw "jarvis_assistant-*.whl was not found next to install.ps1." }

# --- 2. Python 3.11+
function Find-Python {
    foreach ($v in '3.14', '3.13', '3.12', '3.11') {
        try { $p = & py "-$v" -c "import sys; print(sys.executable)" 2>$null; if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() } } catch {}
    }
    try {
        $p = & python -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p -and $p.Trim()) { return $p.Trim() }
    } catch {}
    return $null
}
$python = Find-Python
if (-not $python) {
    Say "Python 3.11 or newer is needed." Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $a = Read-Host "Install Python 3.12 now with winget? (Y/n)"
        if ($a -eq '' -or $a -match '^[yY]') {
            winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
            $python = Find-Python
        }
    }
    if (-not $python) { throw "Install Python 3.12 from https://www.python.org/downloads/ (tick 'Add to PATH'), then run this again." }
}
Say "Using Python: $python" Gray

# --- 3. a private environment + the package and its libraries (~300 MB download the first time)
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Say "Creating Jarvis's Python environment..."
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the environment in $venv" }
}
$vpy = Join-Path $venv 'Scripts\python.exe'
Say "Installing Jarvis and its libraries (this can take a few minutes)..."
& $vpy -m pip install --upgrade pip --disable-pip-version-check -q
& $vpy -m pip install --upgrade --disable-pip-version-check $wheel.FullName
if ($LASTEXITCODE -ne 0) { throw "pip install failed (see the messages above)." }

# --- 4. the `jarvis` command, from any terminal
New-Item -ItemType Directory -Force $bin | Out-Null
# Runs through the (Microsoft-trusted, signed) python.exe rather than pip's generated jarvis.exe launcher:
# Windows Smart App Control blocks that unsigned launcher.
Set-Content -Path (Join-Path $bin 'jarvis.cmd') -Encoding ascii -Value "@echo off`r`ntitle J.A.R.V.I.S.`r`n`"$vpy`" -m jarvis %*"
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }
if (($userPath -split ';') -notcontains $bin) {
    [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $bin).TrimStart(';'), 'User')
    Say "Added $bin to your PATH (open a new terminal to use 'jarvis')." Gray
}
$env:Path += ";$bin"

# --- 5. Desktop shortcut
try {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut((Join-Path $desktop 'Jarvis.lnk'))
    $lnk.TargetPath = $vpy
    $lnk.Arguments = '-m jarvis'
    $lnk.WorkingDirectory = $env:USERPROFILE
    $lnk.Description = 'Start J.A.R.V.I.S.'
    $lnk.Save()
} catch { Say "(Could not create the Desktop shortcut: $_)" Yellow }

Say "`nJarvis is installed. Starting first-time setup...`n" Green
& $vpy -m jarvis setup
Say "`nAll done. Double-click 'Jarvis' on the Desktop, or type  jarvis  in a new terminal." Green
