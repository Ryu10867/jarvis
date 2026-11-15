# Builds the shareable installer:  dist\Jarvis-Installer-<version>.zip
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

# --- the installer folder + zip
$out = Join-Path $dist "Jarvis-Installer-$version"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory $out | Out-Null
Copy-Item $wheel.FullName, (Join-Path $root 'packaging\install.ps1'), (Join-Path $root 'packaging\install.bat'), (Join-Path $root 'packaging\uninstall.bat') $out
Copy-Item (Join-Path $root 'packaging\INSTALL.md') (Join-Path $out 'README.md')
$zip = "$out.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$out\*" -DestinationPath $zip
Remove-Item -Recurse -Force (Join-Path $root 'build')

Write-Host "`nWheel:      $($wheel.FullName)" -ForegroundColor Green
Write-Host "Share this: $zip ($([math]::Round((Get-Item $zip).Length / 1KB)) KB)" -ForegroundColor Green
