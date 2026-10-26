param(
    [string]$Python,
    [switch]$Editable,
    [switch]$NoPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding

if (-not $Python) {
    $oeaBootstrap = Get-Command python -ErrorAction SilentlyContinue
    if (-not $oeaBootstrap) { throw 'Install Python 3.12 or newer, then run this installer again.' }
    $Python = $oeaBootstrap.Source
}
$oeaLayoutCode = @'
import json,sys
from pathlib import Path
if sys.version_info < (3,12):
    raise SystemExit('OEA requires Python 3.12 or newer')
# Bootstrap has no OEA dependencies yet; validate its home without importing OEA.
import os
root=Path(os.environ['OEA_HOME']).expanduser() if os.environ.get('OEA_HOME') else Path.home()/'.oea'
if not root.is_absolute():
    raise SystemExit('OEA_HOME must be an absolute path')
root=root.resolve()
source=Path(sys.argv[1]).resolve()
if root.is_relative_to(source) or any((p/'.git').exists() for p in (root,*root.parents)):
    raise SystemExit('OEA home must be outside source repositories')
if os.name == 'nt':
    import ctypes
    if str(root).startswith('\\\\') or ctypes.windll.kernel32.GetDriveTypeW(str(root.anchor)) == 4:
        raise SystemExit('OEA home must be on a local disk')
for name in ('OneDrive','OneDriveConsumer','OneDriveCommercial'):
    if os.environ.get(name) and root.is_relative_to(Path(os.environ[name]).resolve()):
        raise SystemExit('OEA home must be outside synchronized folders')
print(json.dumps({'home':str(root)}))
'@
$oeaLayoutOutput = & $Python -B -c $oeaLayoutCode $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'Cannot prepare the OEA installation with this Python.' }
$oeaHome = [string](($oeaLayoutOutput | Select-Object -Last 1 | ConvertFrom-Json).home)
$oeaRuntime = Join-Path $oeaHome 'runtime'
$oeaRuntimePython = Join-Path $oeaRuntime 'Scripts\python.exe'
$oeaData = Join-Path $oeaHome 'data'

# Updating an environment must not remove modules used by a live host.
if ((Test-Path -LiteralPath $oeaRuntimePython) -and (Test-Path -LiteralPath (Join-Path $oeaData 'host.lock'))) {
    $oeaCheckLock = @'
import sys
from pathlib import Path
from oea.migration.source import offline_source
with offline_source(Path(sys.argv[1])):
    pass
'@
    & $oeaRuntimePython -B -c $oeaCheckLock $oeaData
    if ($LASTEXITCODE -ne 0) { throw 'Stop the OEA host before updating its runtime. Existing user data has not been changed.' }
}
$oeaCache = Join-Path $oeaHome 'cache'
$null = New-Item -ItemType Directory -Force -Path (Join-Path $oeaCache 'tmp'),(Join-Path $oeaCache 'pip'),(Join-Path $oeaHome 'bin')
$env:TEMP = Join-Path $oeaCache 'tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $oeaCache 'pip'
if (-not (Test-Path -LiteralPath $oeaRuntimePython)) {
    & $Python -B -m venv $oeaRuntime
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the managed OEA runtime.' }
}
$oeaInstallArguments = @('-B','-m','pip','install','--no-compile','-c',(Join-Path $PSScriptRoot 'constraints.txt'))
if ($Editable) { $oeaInstallArguments += '--editable' }
$oeaInstallArguments += ($PSScriptRoot + '[web,mcp,workbench]')
& $oeaRuntimePython @oeaInstallArguments
if ($LASTEXITCODE -ne 0) { throw 'OEA installation did not complete. User data was not moved or removed.' }
$oeaSite = Join-Path $oeaRuntime 'Lib\site-packages'
# Editable installs must never leave bytecode in the source checkout.
[IO.File]::WriteAllText((Join-Path $oeaSite '_oea_runtime.pth'), "import sys; sys.dont_write_bytecode = True`n", [Text.Encoding]::ASCII)
& $oeaRuntimePython -B -m oea paths
if ($LASTEXITCODE -ne 0) { throw 'Installed OEA could not report its standard directories.' }
$oeaBin = Join-Path $oeaHome 'bin'
Copy-Item -LiteralPath (Join-Path $oeaRuntime 'Scripts\oea.exe') -Destination (Join-Path $oeaBin 'oea.exe') -Force
foreach ($oeaLauncher in @('Start-Workspace.ps1','Start-Workspace.cmd')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $oeaLauncher) -Destination (Join-Path $oeaBin $oeaLauncher) -Force
}
if (-not $NoPath) {
    $oeaUserPath = [string][Environment]::GetEnvironmentVariable('Path','User')
    if (@($oeaUserPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $oeaBin.TrimEnd('\') }).Count -eq 0) {
        [Environment]::SetEnvironmentVariable('Path', (($oeaUserPath.TrimEnd(';') + ';' + $oeaBin).TrimStart(';')), 'User')
    }
}
Write-Host 'OEA installed. Open Start-Workspace.cmd, or run oea serve from a new terminal.'
Write-Host 'For Codex MCP, use command: oea, args: mcp. Start the host first.'
if ($Editable) { Write-Host 'Development mode: this runtime imports the repository source directly.' }
