# Builds the shareable installers:  dist\Jarvis-Installer-Windows.zip  and  dist\Jarvis-Installer-macOS.zip
#   powershell -ExecutionPolicy Bypass -File build_package.ps1
#
# Only whitelisted files are packed: the jarvis/ code, the Chrome helper, a copy of config.toml as the default
# settings. Your personal data (data\: face, intruder photos, logs, memory) and the models\ folder are never included;
# each friend's `jarvis setup` downloads the models fresh.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root '.venv\Scripts\python.exe'
$stage = Join-Path $root 'build\stage'
$dist = Join-Path $root 'dist'

$version = (Select-String -Path (Join-Path $root 'packaging\pyproject.toml') -Pattern '^version\s*=\s*"(.+)"').Matches[0].Groups[1].Value
Write-Host "Building jarvis-assistant $version" -ForegroundColor Cyan

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage, $dist | Out-Null

# --- stage the source tree
robocopy (Join-Path $root 'jarvis') (Join-Path $stage 'jarvis') /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS | Out-Null
robocopy (Join-Path $root 'extension') (Join-Path $stage 'jarvis\extension') /E /NFL /NDL /NJH /NJS | Out-Null
Copy-Item (Join-Path $root 'config.toml') (Join-Path $stage 'jarvis\default_config.toml')
Copy-Item (Join-Path $root 'packaging\pyproject.toml') $stage
Copy-Item (Join-Path $root 'packaging\INSTALL.md') (Join-Path $stage 'README.md')

# --- default settings for other people: generic, nothing personal
$cfgPath = Join-Path $stage 'jarvis\default_config.toml'
$cfg = [IO.File]::ReadAllText($cfgPath)
$cfg = $cfg -replace '(?m)^owner_name\s*=\s*".*?"', 'owner_name = "sir"'
$cfg = $cfg -replace '(?m)^city\s*=\s*".*?"', 'city = ""'
[IO.File]::WriteAllText($cfgPath, $cfg, (New-Object Text.UTF8Encoding $false))

# --- build the wheel
Remove-Item (Join-Path $dist 'jarvis_assistant-*.whl') -ErrorAction SilentlyContinue
& $py -m pip wheel $stage --no-deps --disable-pip-version-check -w $dist
if ($LASTEXITCODE -ne 0) { throw "wheel build failed" }
$wheel = Get-ChildItem $dist -Filter "jarvis_assistant-$version-*.whl" | Select-Object -First 1

# --- sanity check: nothing personal slipped in
Add-Type -AssemblyName System.IO.Compression.FileSystem
$z = [IO.Compression.ZipFile]::OpenRead($wheel.FullName)
$names = $z.Entries | ForEach-Object FullName
$z.Dispose()
$bad = $names | Where-Object { $_ -match '(?i)(owner_faces|intruder|\.npy$|\.jpg$|jarvis\.log|alerts\.jsonl|memory\.json|learned_commands|\.onnx$)' }
if ($bad) { throw "personal or model files found in the wheel: $($bad -join ', ')" }

# --- the two installer zips (Windows + macOS). Fixed names, so the website's "latest release" links never change.
Write-Host "`nInstallers:" -ForegroundColor Green
& $py (Join-Path $root 'packaging\make_zips.py') $version
if ($LASTEXITCODE -ne 0) { throw "packing the installers failed" }
Write-Host "Website:" -ForegroundColor Green
& $py (Join-Path $root 'packaging\make_site.py')
if ($LASTEXITCODE -ne 0) { throw "building the website failed" }
Remove-Item -Recurse -Force (Join-Path $root 'build')

Write-Host "Wheel:  $($wheel.FullName)" -ForegroundColor Green
Write-Host "Upload the two zips (and the wheel) to a GitHub release tagged v$version." -ForegroundColor Green
