param(
    [switch]$Mock,
    [ValidateRange(1, 65535)][int]$Port = 8890,
    [Nullable[int]]$ReservedExternalSlots = $null,
    [string]$DataDir,
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding
$maSourceRoot = $PSScriptRoot
$maEnvironmentRoot = Join-Path $env:LOCALAPPDATA 'OEA\environment'
$maPython = Join-Path $maEnvironmentRoot 'Scripts\python.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'

if (-not $DataDir) {
    $DataDir = Join-Path $env:LOCALAPPDATA 'OEA\workspace'
    $maLegacyData = Join-Path $env:LOCALAPPDATA 'MultiAgentPlatform\integrated-workspace'
    if (-not (Test-Path -LiteralPath (Join-Path $DataDir 'runtime.sqlite')) -and
        (Test-Path -LiteralPath (Join-Path $maLegacyData 'runtime.sqlite'))) {
        $DataDir = $maLegacyData
    }
    # A desktop-app package may redirect LOCALAPPDATA. Explorer must still find
    # the workspace that was created from within Codex instead of an empty one.
    if (-not (Test-Path -LiteralPath (Join-Path $DataDir 'runtime.sqlite'))) {
        $maPackages = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Packages'
        $maPackagedRoots = @(Get-ChildItem -LiteralPath $maPackages -Directory -Filter 'OpenAI.Codex_*' -ErrorAction SilentlyContinue |
            ForEach-Object {
                Join-Path $_.FullName 'LocalCache\Local\OEA\workspace'
                Join-Path $_.FullName 'LocalCache\Local\MultiAgentPlatform\integrated-workspace'
            } |
            Where-Object { Test-Path -LiteralPath (Join-Path $_ 'runtime.sqlite') })
        if ($maPackagedRoots.Count -gt 1) {
            throw 'Multiple existing workspaces were found in desktop-app storage. Select one with -DataDir.'
        }
        if ($maPackagedRoots.Count -eq 1) {
            $DataDir = $maPackagedRoots[0]
            $maPackagedEnvironment = Join-Path (Split-Path -Parent $DataDir) 'environment'
            if (-not (Test-Path -LiteralPath $maPython) -and
                (Test-Path -LiteralPath (Join-Path $maPackagedEnvironment 'Scripts\python.exe'))) {
                $maEnvironmentRoot = $maPackagedEnvironment
                $maPython = Join-Path $maEnvironmentRoot 'Scripts\python.exe'
            }
        }
    }
}
$maDataRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DataDir)
$maUrl = "http://127.0.0.1:$Port"
$maClientPath = Join-Path $maDataRoot 'client.json'
$maTokenPath = Join-Path $maDataRoot 'client-token'

function Test-WorkspaceService([string]$Url) {
    if (-not (Test-Path -LiteralPath $maTokenPath)) { return $false }
    try {
        $maToken = (Get-Content -LiteralPath $maTokenPath -Raw -Encoding UTF8).Trim()
        if (-not $maToken) { return $false }
        $maProtocol = Invoke-RestMethod -Uri "$Url/api/protocol" -Method Get `
            -Headers @{ Authorization = "Bearer $maToken" } -TimeoutSec 2 -MaximumRedirection 0
        return $maProtocol.name -in @('Orchestrate Everything with Agents', 'Multi-Agent Platform') -and $maProtocol.capabilities -contains 'mcp_registry'
    } catch {
        return $false
    }
}

function Show-Workspace([string]$Url) {
    Write-Host "Workspace: $Url"
    Write-Host "Data:      $maDataRoot"
    if (-not $NoBrowser) {
        try { Start-Process -FilePath "$Url/#mcp" }
        catch { Write-Warning "Could not open the browser. Open $Url/#mcp manually." }
    }
}

# Authenticate with this workspace's token before reusing a running instance.
# Never follow an arbitrary URL from client metadata or send a token remotely.
if (Test-Path -LiteralPath $maClientPath) {
    $maSavedUrl = $null
    try {
        $maMetadata = Get-Content -LiteralPath $maClientPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $maSavedUri = [uri]$maMetadata.url
        if ($maSavedUri.Scheme -eq 'http' -and
            $maSavedUri.Host -in @('127.0.0.1', 'localhost', '[::1]', '::1') -and
            -not $maSavedUri.UserInfo -and -not $maSavedUri.Query -and -not $maSavedUri.Fragment -and
            $maSavedUri.AbsolutePath -eq '/') {
            $maSavedUrl = $maSavedUri.GetLeftPart([System.UriPartial]::Authority)
        }
    } catch { }
    if ($maSavedUrl -and (Test-WorkspaceService $maSavedUrl)) {
        if ($PSBoundParameters.ContainsKey('Port') -and $maSavedUri.Port -ne $Port) {
            throw "This workspace is already running at $maSavedUrl. Stop that instance before changing its port."
        }
        if ($Mock -or $null -ne $ReservedExternalSlots) {
            Write-Warning 'Reusing the running workspace; Mock and ReservedExternalSlots only apply on a fresh start.'
        }
        Write-Host 'Using the running workspace.'
        Show-Workspace $maSavedUrl
        exit 0
    }
}

# Detect port conflicts before opening the database or installing dependencies.
$maProbe = New-Object System.Net.Sockets.TcpClient
try {
    $maConnect = $maProbe.ConnectAsync('127.0.0.1', $Port)
    try { $null = $maConnect.Wait(1000) } catch { }
    if ($maProbe.Connected) {
        throw "Port $Port is in use by another service or workspace. Choose -Port with a free port."
    }
} finally { $maProbe.Dispose() }

if (-not (Test-Path -LiteralPath $maPython)) {
    & python -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12 or newer is required"'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or newer is required.' }
    & python -m venv $maEnvironmentRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the external Python environment.' }
}

$maDependencyHash = (Get-FileHash -LiteralPath (Join-Path $maSourceRoot 'pyproject.toml') -Algorithm SHA256).Hash +
    (Get-FileHash -LiteralPath (Join-Path $maSourceRoot 'constraints.txt') -Algorithm SHA256).Hash + $maSourceRoot
$maInstallMarker = Join-Path $maEnvironmentRoot 'source-install.hash'
$maInstalledHash = if (Test-Path -LiteralPath $maInstallMarker) { Get-Content -LiteralPath $maInstallMarker -Raw -Encoding UTF8 } else { '' }
if ($maInstalledHash.Trim() -ne $maDependencyHash) {
    & $maPython -m pip install --disable-pip-version-check --constraint (Join-Path $maSourceRoot 'constraints.txt') --editable "$maSourceRoot[web,mcp]"
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; the source tree has not been modified.' }
    Set-Content -LiteralPath $maInstallMarker -Value $maDependencyHash -Encoding UTF8
}

# Validate and normalize state paths with the same rules as the Runtime.
$maValidation = 'import sys; from ma_runtime.paths import external_path, require_local_state; p = external_path(sys.argv[1]); require_local_state(p); print(p)'
$maValidatedPath = & $maPython -c $maValidation $maDataRoot
if ($LASTEXITCODE -ne 0) { throw 'The data directory must be on a local disk outside the source repository.' }
$maDataRoot = [string]($maValidatedPath | Select-Object -Last 1)
$maLogRoot = Join-Path $maDataRoot 'logs'
$null = New-Item -ItemType Directory -Path $maLogRoot -Force
$maLaunchId = Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
$maOutputLog = Join-Path $maLogRoot "launcher-$maLaunchId.out.log"
$maErrorLog = Join-Path $maLogRoot "launcher-$maLaunchId.err.log"

$maArguments = @('-m', 'oea', 'serve', '--port', [string]$Port, '--data-dir', $maDataRoot)
if ($Mock) { $maArguments += '--mock' }
if ($null -ne $ReservedExternalSlots) { $maArguments += @('--reserved-external-slots', [string]$ReservedExternalSlots) }
# Start-Process joins its argument array; quote according to Windows argv rules.
$maCommandLine = ($maArguments | ForEach-Object {
    '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}) -join ' '
Write-Host 'Starting Orchestrate Everything with Agents...'
$maService = Start-Process -FilePath $maPython -ArgumentList $maCommandLine `
    -WorkingDirectory $maDataRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $maOutputLog -RedirectStandardError $maErrorLog
$maDeadline = [DateTime]::UtcNow.AddSeconds(45)
do {
    if (Test-WorkspaceService $maUrl) {
        Write-Host 'Workspace is ready. The service runs in the background.'
        Show-Workspace $maUrl
        exit 0
    }
    $maService.Refresh()
    if ($maService.HasExited) {
        throw "The workspace did not start (exit $($maService.ExitCode)). See $maErrorLog"
    }
    Start-Sleep -Milliseconds 400
} while ([DateTime]::UtcNow -lt $maDeadline)
throw "The workspace has not become ready yet. See $maErrorLog before trying again."
