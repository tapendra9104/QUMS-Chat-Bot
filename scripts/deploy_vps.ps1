param(
    [Parameter(Mandatory = $true)]
    [string]$Host,

    [string]$User = "root",

    [int]$Port = 22,

    [string]$AppDir = "/opt/qums-bot",

    [string]$ServiceName = "qums-bot",

    [string]$AppUser = "qumsbot",

    [string]$ServerName = "",

    [string]$PublicBaseUrl = "",

    [string]$Timezone = "Asia/Kolkata",

    [string]$IdentityFile = "",

    [string]$RemoteTmpDir = "/tmp/qums-bot-deploy",

    [string]$EnvPath = ".env",

    [switch]$UploadEnv,

    [switch]$UseSudo
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Quote-Bash {
    param([Parameter(Mandatory = $true)][string]$Value)
    return "'" + $Value.Replace("'", "'\"'\"'") + "'"
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $Command $($Arguments -join ' ')"
    }
}

if (-not $ServerName) {
    $ServerName = $Host
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$bootstrapPath = Join-Path $repoRoot "scripts\bootstrap_vps.sh"
if (-not (Test-Path $bootstrapPath)) {
    throw "Bootstrap script not found at $bootstrapPath"
}

$archivePath = Join-Path $env:TEMP ("qums-bot-" + (Get-Date -Format "yyyyMMddHHmmss") + ".tar.gz")
$remoteArchive = "$RemoteTmpDir/app.tar.gz"
$remoteBootstrap = "$RemoteTmpDir/bootstrap_vps.sh"
$remoteEnv = "$RemoteTmpDir/.env"
$sshTarget = "$User@$Host"

$sshCommon = @("-p", "$Port", "-o", "StrictHostKeyChecking=accept-new")
$scpCommon = @("-P", "$Port", "-o", "StrictHostKeyChecking=accept-new")
if ($IdentityFile) {
    $sshCommon += @("-i", $IdentityFile)
    $scpCommon += @("-i", $IdentityFile)
}

$excludeArgs = @(
    "--exclude=.git",
    "--exclude=.venv",
    "--exclude=.pytest_cache",
    "--exclude=__pycache__",
    "--exclude=backups",
    "--exclude=data",
    "--exclude=logs"
)
if (-not $UploadEnv) {
    $excludeArgs += "--exclude=.env"
}

Push-Location $repoRoot
try {
    Invoke-Native tar "-czf" $archivePath @excludeArgs "."
} finally {
    Pop-Location
}

try {
    Invoke-Native ssh @sshCommon $sshTarget "mkdir -p $(Quote-Bash $RemoteTmpDir)"
    Invoke-Native scp @scpCommon $archivePath "${sshTarget}:$remoteArchive"
    Invoke-Native scp @scpCommon $bootstrapPath "${sshTarget}:$remoteBootstrap"

    if ($UploadEnv) {
        $resolvedEnvPath = (Resolve-Path (Join-Path $repoRoot $EnvPath)).Path
        Invoke-Native scp @scpCommon $resolvedEnvPath "${sshTarget}:$remoteEnv"
    }

    $envPairs = @(
        "APP_DIR=$(Quote-Bash $AppDir)",
        "SERVICE_NAME=$(Quote-Bash $ServiceName)",
        "APP_USER=$(Quote-Bash $AppUser)",
        "SOURCE_MODE=archive",
        "ARCHIVE_PATH=$(Quote-Bash $remoteArchive)",
        "SERVER_NAME=$(Quote-Bash $ServerName)",
        "LOCAL_TIMEZONE_DEFAULT=$(Quote-Bash $Timezone)"
    )
    if ($PublicBaseUrl) {
        $envPairs += "PUBLIC_BASE_URL_DEFAULT=$(Quote-Bash $PublicBaseUrl)"
    }
    if ($UploadEnv) {
        $envPairs += "ENV_FILE_SOURCE=$(Quote-Bash $remoteEnv)"
    }

    $remoteCommand = ($envPairs + @("bash $(Quote-Bash $remoteBootstrap)")) -join " "
    if ($UseSudo) {
        $remoteCommand = "sudo env $remoteCommand"
    }

    Invoke-Native ssh @sshCommon -t $sshTarget $remoteCommand
    Invoke-Native ssh @sshCommon $sshTarget "rm -rf $(Quote-Bash $RemoteTmpDir)"
}
finally {
    if (Test-Path $archivePath) {
        Remove-Item $archivePath -Force
    }
}
