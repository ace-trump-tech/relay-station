param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& $Python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install packaging dependencies.' }
& $Python packaging/collect_licenses.py
if ($LASTEXITCODE -ne 0) { throw 'Could not collect third-party license notices.' }
& $Python -m PyInstaller --noconfirm Relay.spec
if ($LASTEXITCODE -ne 0) { throw 'Could not build Relay.exe.' }
Write-Output "Built: $PSScriptRoot/dist/Relay.exe"
