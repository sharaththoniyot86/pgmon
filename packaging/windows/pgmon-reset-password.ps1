#Requires -Version 5.1
<#
.SYNOPSIS
    Resets a pgmon login account's password on a standalone Windows install.
.DESCRIPTION
    There's no in-app "forgot password" flow, deliberately -- doing it from
    inside the app would mean any admin session could silently take over any
    other account. This is the Windows equivalent of the Linux installer's
    pgmon-reset-password command: it needs administrator access to the
    server pgmon runs on instead. If the named account doesn't exist, it's
    created fresh as an admin instead of failing -- covers losing every
    account, not just forgetting one password.
.EXAMPLE
    .\pgmon-reset-password.ps1
    Resets 'admin', prompting for the new password.
.EXAMPLE
    .\pgmon-reset-password.ps1 alice
    Resets (or creates) a specific account.
.EXAMPLE
    'newpassword' | .\pgmon-reset-password.ps1 admin
    Non-interactive, for scripting -- also how the Account tab's "Reset
    password..." button builds this command.
#>
param(
    [Parameter(Position = 0)]
    [string]$Username = "admin",

    [Parameter(ValueFromPipeline = $true)]
    [string]$PipedPassword
)

$ErrorActionPreference = "Stop"
$InstallDir = $PSScriptRoot
$PythonExe = Join-Path $InstallDir "python\python.exe"
$ConfigDir = Join-Path $env:ProgramData "pgmon"
$EnvFile = Join-Path $ConfigDir "pgmon.env"

function Assert-Admin {
    if (-not $IsWindows -and $null -ne (Get-Variable -Name IsWindows -ErrorAction SilentlyContinue)) {
        return # non-Windows PowerShell (test/dev only) -- skip the Windows-only elevation check
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
        Write-Error "Run this from an elevated PowerShell prompt (Right-click -> Run as Administrator)."
        exit 1
    }
}

Assert-Admin

if (-not (Test-Path $PythonExe)) {
    Write-Error "pgmon doesn't look installed at $InstallDir (no python\python.exe) -- is this the right machine?"
    exit 1
}
if (-not (Test-Path $EnvFile)) {
    Write-Error "Missing $EnvFile -- has the pgmon service started at least once?"
    exit 1
}

# Non-interactive: piped a single line (the Account tab's generated command
# does this). Interactive: prompt twice with masked input and confirm match.
if ($PipedPassword) {
    $Password = $PipedPassword
} else {
    $sec1 = Read-Host "New password for '$Username'" -AsSecureString
    $sec2 = Read-Host "Confirm" -AsSecureString
    $plain1 = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec1))
    $plain2 = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec2))
    if ($plain1 -ne $plain2) {
        Write-Error "Passwords didn't match."
        exit 1
    }
    $Password = $plain1
}
if ([string]::IsNullOrEmpty($Password)) {
    Write-Error "Password can't be empty."
    exit 1
}

# Load pgmon.env into this process's environment so create_user can reach
# the same USERS_DB_PATH/SESSION_SECRET the running service uses.
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $key, $value = $line.Split("=", 2)
        if (-not [System.Environment]::GetEnvironmentVariable($key)) {
            [System.Environment]::SetEnvironmentVariable($key, $value)
        }
    }
}

Push-Location $InstallDir
try {
    $created = $false
    $Password | & $PythonExe -m app.create_user $Username - --update
    if ($LASTEXITCODE -ne 0) {
        $Password | & $PythonExe -m app.create_user $Username -
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to set the password -- check the pgmon service's Event Log output and that $EnvFile is intact."
            exit 1
        }
        $created = $true
    }
} finally {
    Pop-Location
}

if ($created) {
    Write-Host "No existing account named '$Username' -- created it fresh as an admin."
} else {
    Write-Host "Password updated for '$Username'."
}
