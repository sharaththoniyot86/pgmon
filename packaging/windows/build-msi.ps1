#Requires -Version 5.1
<#
.SYNOPSIS
    Builds pgmon-<version>.msi -- a self-contained Windows installer bundling
    its own Python runtime (the official embeddable distribution), every pip
    dependency (pre-downloaded as wheels, no PyPI access needed mid-build
    beyond this machine's own), and WinSW as the Windows-service wrapper
    (plain python.exe doesn't speak the Windows Service Control Protocol;
    WinSW bridges SCM start/stop/crash signals to it -- same role Nginx+
    gunicorn play on Linux, minus the reverse proxy since Uvicorn alone
    serves HTTP directly here).

    Must run on Windows (WiX only builds there -- see packaging/windows/README.md
    for why) with the WiX CLI installed: dotnet tool install --global wix,
    then wix extension add WixToolset.UI.wixext.
.PARAMETER Version
    MSI product version, e.g. 1.0.0.
.PARAMETER Port
    Port Uvicorn binds to inside the service. Default 8080.
#>
param(
    [string]$Version = "1.0.0",
    [string]$PythonVersion = "3.12.10",
    [string]$WinSwVersion = "2.12.0",
    [int]$Port = 8080
)
$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$Stage = Join-Path $ScriptDir "stage"
$DistDir = Join-Path $RepoRoot "dist"

Write-Host "==> Staging pgmon $Version..."
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Path $Stage | Out-Null
New-Item -ItemType Directory -Path $DistDir -Force | Out-Null

# ------------------------------------------------------------------ app code
Copy-Item -Recurse (Join-Path $RepoRoot "app") (Join-Path $Stage "app")
Get-ChildItem -Recurse -Directory -Filter "__pycache__" (Join-Path $Stage "app") |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName }
Copy-Item (Join-Path $ScriptDir "service_launcher.py") (Join-Path $Stage "service_launcher.py")
Copy-Item (Join-Path $ScriptDir "pgmon-reset-password.ps1") (Join-Path $Stage "pgmon-reset-password.ps1")

# ------------------------------------------------------- embeddable Python
Write-Host "==> Downloading Python $PythonVersion embeddable runtime..."
$PyZip = Join-Path $env:TEMP "pgmon-python-embed.zip"
Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $PyZip
$PythonStage = Join-Path $Stage "python"
Expand-Archive -Path $PyZip -DestinationPath $PythonStage -Force

# Enable site-packages: the embeddable distro ships a pythonXY._pth that
# restricts sys.path to just itself + its bundled stdlib zip by default, and
# comments out `import site` (so .dist-info metadata / namespace packages
# some of our dependencies rely on wouldn't work without this).
$PthFile = Get-ChildItem (Join-Path $PythonStage "python3*._pth") | Select-Object -First 1
$pthContent = Get-Content $PthFile.FullName
$pthContent = $pthContent -replace '^#\s*import site$', 'import site'
$pthContent += "Lib\site-packages"
Set-Content -Path $PthFile.FullName -Value $pthContent

# ---------------------------------------------- vendor every pip dependency
Write-Host "==> Vendoring pip dependencies for win_amd64/cp312 (this machine needs internet; the target machine won't)..."
$SitePackages = Join-Path $PythonStage "Lib\site-packages"
New-Item -ItemType Directory -Path $SitePackages -Force | Out-Null
$WheelDir = Join-Path $env:TEMP "pgmon-wheels"
if (Test-Path $WheelDir) { Remove-Item -Recurse -Force $WheelDir }
New-Item -ItemType Directory -Path $WheelDir | Out-Null

$ReqFile = Join-Path $RepoRoot "requirements-windows.txt"
& pip download --no-cache-dir -d $WheelDir `
    --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 --only-binary=:all: `
    -r $ReqFile
if ($LASTEXITCODE -ne 0) { throw "pip download failed" }

& pip install --no-cache-dir --no-index --find-links $WheelDir `
    --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 --only-binary=:all: `
    --target $SitePackages `
    -r $ReqFile
if ($LASTEXITCODE -ne 0) { throw "pip install --target failed" }

# pip's --target drops a POSIX-flavored bin/ (shebang launcher scripts, not
# usable on Windows anyway -- we invoke uvicorn via service_launcher.py, not
# a generated console-script exe) and __pycache__; neither belongs in the MSI.
Remove-Item -Recurse -Force (Join-Path $SitePackages "bin") -ErrorAction SilentlyContinue
Get-ChildItem -Recurse -Directory -Filter "__pycache__" $SitePackages |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName }

# ------------------------------------------------------------------- WinSW
Write-Host "==> Downloading WinSW $WinSwVersion (Windows service wrapper)..."
Invoke-WebRequest -Uri "https://github.com/winsw/winsw/releases/download/v$WinSwVersion/WinSW-x64.exe" `
    -OutFile (Join-Path $Stage "pgmon-service.exe")
Copy-Item (Join-Path $ScriptDir "pgmon-service.xml") (Join-Path $Stage "pgmon-service.xml")

# --------------------------------------------- harvest staged files for WiX
# Hand-rolled instead of WiX's newer <Files Include> globbing shortcut: this
# script can't be compile-tested on Linux (WiX only runs on Windows), so it
# sticks to the oldest, most stable primitives (Directory/Component/File,
# stable since WiX v3) rather than a newer shortcut this has no way to
# verify ahead of a real Windows/CI run.
$script:NextId = 0
function New-WxsId([string]$Prefix) {
    $script:NextId++
    return "$Prefix$($script:NextId)"
}

$script:ComponentIds = New-Object System.Collections.Generic.List[string]

function Build-DirectoryXml([string]$Path, [string]$Indent) {
    $sb = New-Object System.Text.StringBuilder
    $items = Get-ChildItem -LiteralPath $Path -Force
    foreach ($f in ($items | Where-Object { -not $_.PSIsContainer })) {
        $fileId = New-WxsId "f"
        $compId = New-WxsId "c"
        $script:ComponentIds.Add($compId) | Out-Null
        $escapedSrc = $f.FullName.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace('"', "&quot;")
        $escapedName = $f.Name.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace('"', "&quot;")
        [void]$sb.AppendLine("$Indent<Component Id=`"$compId`" Guid=`"*`">")
        [void]$sb.AppendLine("$Indent  <File Id=`"$fileId`" Source=`"$escapedSrc`" Name=`"$escapedName`" KeyPath=`"yes`" />")
        [void]$sb.AppendLine("$Indent</Component>")
    }
    foreach ($d in ($items | Where-Object { $_.PSIsContainer })) {
        $dirId = New-WxsId "d"
        $escapedDirName = $d.Name.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace('"', "&quot;")
        [void]$sb.AppendLine("$Indent<Directory Id=`"$dirId`" Name=`"$escapedDirName`">")
        [void]$sb.Append((Build-DirectoryXml -Path $d.FullName -Indent "$Indent  "))
        [void]$sb.AppendLine("$Indent</Directory>")
    }
    return $sb.ToString()
}

Write-Host "==> Harvesting staged files for WiX authoring..."
# pgmon-service.exe/.xml get their own hand-authored Component (with
# ServiceInstall/ServiceControl attached) in the static template below, so
# the generic harvester must skip them here to avoid a duplicate/competing
# Component for the same two files.
$HarvestRoot = Join-Path $Stage "_harvest"
New-Item -ItemType Directory -Path $HarvestRoot | Out-Null
Get-ChildItem -LiteralPath $Stage -Force | Where-Object {
    $_.Name -notin @("pgmon-service.exe", "pgmon-service.xml", "_harvest")
} | ForEach-Object {
    Move-Item -LiteralPath $_.FullName -Destination $HarvestRoot
}
$generatedComponentsXml = Build-DirectoryXml -Path $HarvestRoot -Indent "        "
$generatedComponentRefsXml = ($script:ComponentIds | ForEach-Object { "      <ComponentRef Id=`"$_`" />" }) -join "`n"
Write-Host "    ($($script:ComponentIds.Count) files)"

# ---------------------------------------------------------------- the .wxs
$upgradeCode = "298C3E07-9F75-4E96-B251-87A8920453C4"
$wxs = @"
<?xml version="1.0" encoding="UTF-8"?>
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs"
     xmlns:ui="http://wixtoolset.org/schemas/v4/wxs/ui">
  <Package Name="pgmon" Manufacturer="pgmon" Version="$Version" UpgradeCode="$upgradeCode"
           Scope="perMachine" InstallerVersion="500">
    <SummaryInformation Description="pgmon - AI Postgres Monitor" Manufacturer="pgmon" />
    <MajorUpgrade DowngradeErrorMessage="A newer version of pgmon is already installed." />
    <MediaTemplate EmbedCab="yes" />

    <StandardDirectory Id="ProgramFiles64Folder">
      <Directory Id="INSTALLFOLDER" Name="pgmon">
        <Component Id="ServiceExeComponent" Guid="*">
          <File Id="ServiceExeFile" Source="$Stage\pgmon-service.exe" Name="pgmon-service.exe" KeyPath="yes" />
          <File Id="ServiceXmlFile" Source="$Stage\pgmon-service.xml" Name="pgmon-service.xml" />
          <ServiceInstall Id="PgmonServiceInstall"
                          Name="pgmon"
                          DisplayName="pgmon - AI Postgres Monitor"
                          Description="AI-powered PostgreSQL monitoring dashboard"
                          Type="ownProcess"
                          Start="auto"
                          ErrorControl="normal" />
          <ServiceControl Id="PgmonServiceControl"
                          Name="pgmon"
                          Start="install"
                          Stop="both"
                          Remove="uninstall"
                          Wait="yes" />
        </Component>
$generatedComponentsXml
      </Directory>
    </StandardDirectory>

    <Feature Id="MainFeature" Title="pgmon" Level="1">
      <ComponentRef Id="ServiceExeComponent" />
$generatedComponentRefsXml
    </Feature>

    <ui:WixUI Id="WixUI_Minimal" />
    <WixVariable Id="WixUILicenseRtf" Value="$ScriptDir\License.rtf" />
  </Package>
</Wix>
"@

$WxsPath = Join-Path $Stage "pgmon.wxs"
Set-Content -Path $WxsPath -Value $wxs -Encoding UTF8

Write-Host "==> Compiling MSI with WiX..."
$MsiOut = Join-Path $DistDir "pgmon-$Version.msi"
& wix build $WxsPath -arch x64 -ext WixToolset.UI.wixext -o $MsiOut
if ($LASTEXITCODE -ne 0) { throw "wix build failed" }

Write-Host "==> Built $MsiOut"
