param(
    [string]$DataDir,
    [string]$Python,
    [ValidateRange(1, 65535)][int]$Port = 8890,
    [switch]$NoBrowser,
    [switch]$AllowCommands
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'

function Stop-Launcher([string]$Message) {
    Write-Host $Message -ForegroundColor Red
    exit 1
}
if (-not $Python) {
    $oeaHome = if ($env:OEA_HOME) { $env:OEA_HOME } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.oea' }
    if (-not [IO.Path]::IsPathRooted($oeaHome)) { Stop-Launcher 'OEA_HOME must be an absolute path.' }
    $Python = Join-Path $oeaHome 'runtime\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python)) { Stop-Launcher 'OEA is not installed. Run Install-OEA.cmd once, then open Start-Workspace.cmd.' }
}
# Windows PowerShell can omit an empty native argument; absence selects the package default.
# Return validation failures on stdout so native stderr does not bypass our startup message.
$oeaValidate = @'
import json, sys
try:
    from oea.contracts.errors import RuntimeFault
    from oea.workspace.paths import resolve_data_dir
except ImportError as error:
    print(json.dumps({'error': 'OEA or a required dependency is not installed in the selected Python environment: ' + str(error.name)}))
    raise SystemExit(2)
try:
    path = resolve_data_dir(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None)
except RuntimeFault as error:
    print(json.dumps({'error': error.safe_message, 'code': error.code}))
    raise SystemExit(3)
print(json.dumps({'path': str(path)}))
'@
try {
    $oeaValidated = & $Python -B -c $oeaValidate ([string]$DataDir)
    $oeaValidationExitCode = $LASTEXITCODE
} catch {
    Stop-Launcher "Cannot run OEA Python '$Python'. Run Install-OEA.cmd to prepare the managed runtime. $($_.Exception.Message)"
}
try { $oeaValidation = ([string]($oeaValidated | Select-Object -Last 1)) | ConvertFrom-Json }
catch { Stop-Launcher "Python '$Python' could not validate the OEA installation. See docs/getting-started.md and check that OEA 2 is installed in this environment." }
if ($oeaValidationExitCode -ne 0) {
    $oeaReason = [string]$oeaValidation.error
    if ($oeaValidationExitCode -eq 2) {
        Stop-Launcher "Python '$Python': $oeaReason. Run Install-OEA.cmd, or install into an explicitly selected development environment with pip install -e '.[web,mcp,workbench]'."
    }
    Stop-Launcher "Python '$Python': $oeaReason. Select a local data directory outside source repositories and synchronized folders."
}
$oeaDataRoot = [string]$oeaValidation.path
if ((Test-Path -LiteralPath (Join-Path $oeaDataRoot 'runtime.sqlite')) -and -not (Test-Path -LiteralPath (Join-Path $oeaDataRoot 'oea.sqlite'))) {
    throw 'Legacy state requires offline conversion. Run oea migrate --source OLD_DIRECTORY first, then --target NEW_DIRECTORY --apply after reviewing the report. Start with the new directory.'
}
$oeaUrl = "http://127.0.0.1:$Port"
$oeaTokenPath = Join-Path $oeaDataRoot 'client-token'
$oeaClientPath = Join-Path $oeaDataRoot 'client.json'

function Test-WorkspaceService([string]$Url) {
    if (-not (Test-Path -LiteralPath $oeaTokenPath)) { return $false }
    try {
        $token = (Get-Content -LiteralPath $oeaTokenPath -Raw -Encoding UTF8).Trim()
        if (-not $token) { return $false }
        $protocol = Invoke-RestMethod -Uri "$Url/api/protocol" -Method Get -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 2 -MaximumRedirection 0
        return $protocol.protocol_version -eq '2.0' -and $protocol.engine -eq 'langgraph'
    } catch { return $false }
}
function Show-Workspace([string]$Url) {
    Write-Host "Workspace: $Url"
    Write-Host "Data:      $oeaDataRoot"
    if (-not $NoBrowser) {
        try { Start-Process -FilePath $Url }
        catch { Write-Warning "Open $Url in your browser." }
    }
}
# Only authenticate against loopback metadata owned by this data directory.
if (Test-Path -LiteralPath $oeaClientPath) {
    $oeaSavedUrl = $null
    try {
        $oeaMetadata = Get-Content -LiteralPath $oeaClientPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $oeaSavedUri = [uri]$oeaMetadata.url
        if ($oeaSavedUri.Scheme -eq 'http' -and $oeaSavedUri.Host -in @('127.0.0.1','localhost','[::1]','::1') -and -not $oeaSavedUri.UserInfo -and -not $oeaSavedUri.Query -and -not $oeaSavedUri.Fragment -and $oeaSavedUri.AbsolutePath -eq '/') {
            $oeaSavedUrl = $oeaSavedUri.GetLeftPart([System.UriPartial]::Authority)
        }
    } catch { }
    if ($oeaSavedUrl -and -not $PSBoundParameters.ContainsKey('Port')) {
        $Port = $oeaSavedUri.Port
        $oeaUrl = "http://127.0.0.1:$Port"
    }
    if ($oeaSavedUrl -and (Test-WorkspaceService $oeaSavedUrl)) {
        if ($PSBoundParameters.ContainsKey('Port') -and $oeaSavedUri.Port -ne $Port) { throw "This data directory is already served at $oeaSavedUrl. Stop that host before changing its port." }
        if ($AllowCommands) { Write-Host 'Reusing the host with its existing command grant.' }
        Show-Workspace $oeaSavedUrl
        exit 0
    }
}
$oeaProbe = New-Object System.Net.Sockets.TcpClient
try {
    $oeaConnect = $oeaProbe.ConnectAsync('127.0.0.1', $Port)
    try { $null = $oeaConnect.Wait(1000) } catch { }
    if ($oeaProbe.Connected) { throw "Port $Port is in use. Select a free -Port." }
} finally { $oeaProbe.Dispose() }
$oeaLogRoot = Join-Path $oeaDataRoot 'logs'
$null = New-Item -ItemType Directory -Force -Path $oeaLogRoot
$oeaLaunch = Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
$oeaOutputLog = Join-Path $oeaLogRoot "launcher-$oeaLaunch.out.log"
$oeaErrorLog = Join-Path $oeaLogRoot "launcher-$oeaLaunch.err.log"
$oeaArguments = @('-B', '-m', 'oea', 'serve', '--port', [string]$Port, '--data-dir', $oeaDataRoot)
if ($AllowCommands) { $oeaArguments += '--allow-commands' }
# Start-Process joins its array; preserve Windows argv quoting and empty values.
$oeaCommandLine = ($oeaArguments | ForEach-Object { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }) -join ' '
$oeaService = Start-Process -FilePath $Python -ArgumentList $oeaCommandLine -WorkingDirectory $oeaDataRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $oeaOutputLog -RedirectStandardError $oeaErrorLog
Write-Host "Host PID:  $($oeaService.Id)"
$oeaDeadline = [DateTime]::UtcNow.AddSeconds(45)
do {
    if (Test-WorkspaceService $oeaUrl) { Show-Workspace $oeaUrl; exit 0 }
    $oeaService.Refresh()
    if ($oeaService.HasExited) { throw "The host exited ($($oeaService.ExitCode)). See $oeaErrorLog" }
    Start-Sleep -Milliseconds 400
} while ([DateTime]::UtcNow -lt $oeaDeadline)
throw "The host has not become ready. Read $oeaErrorLog before retrying."
