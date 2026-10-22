# pgmon standalone Windows install

Installs pgmon as a Windows service on a plain Windows machine -- no Docker,
Kubernetes, or separately-installed Python required. The MSI bundles:

- its own Python 3.12 runtime (the official embeddable distribution) and
  every pip dependency, pre-downloaded as wheels at build time -- nothing
  needs internet access on the machine being installed on
- [WinSW](https://github.com/winsw/winsw) as the Windows-service wrapper
  (plain `python.exe` doesn't speak the Windows Service Control Protocol;
  WinSW bridges SCM start/stop/crash signals to it -- the Windows
  equivalent of what Nginx+gunicorn do on Linux, minus the reverse proxy:
  **Uvicorn alone serves HTTP directly** here, by design -- see the main
  conversation's "Web server" choice)

## Installing

Double-click `pgmon-1.0.0.msi` (or `msiexec /i pgmon-1.0.0.msi` from an
elevated prompt for a silent/scripted install: add `/quiet` to skip the
wizard). This:

- installs to `C:\Program Files\pgmon\`
- registers and starts the **pgmon** Windows service (Services.msc), bound
  to port 8080 by default
- on the service's first start, generates a config file at
  `C:\ProgramData\pgmon\pgmon.env` (session secret, DB path, etc.) and
  creates an initial `admin` account with a random password, written once to
  `C:\ProgramData\pgmon\initial-admin-password.txt` -- read it, sign in,
  change the password, then delete that file

Browse to `http://localhost:8080/` (or `http://<machine-name>:8080/` from
another machine on the network, once your firewall allows the port).

## Where things live

| Path | Contents |
|---|---|
| `C:\Program Files\pgmon\` | application code, bundled Python runtime, WinSW (replaced on upgrade) |
| `C:\ProgramData\pgmon\pgmon.env` | configuration (session secret, feature toggles) |
| `C:\ProgramData\pgmon\data\pgmon_users.db` | the accounts/settings SQLite DB -- back this up |

```powershell
Get-Service pgmon                 # or Start-Service / Stop-Service / Restart-Service
Get-Content "C:\Program Files\pgmon\pgmon.out.log" -Tail 50 -Wait   # app output (WinSW's <id> is "pgmon" -- see pgmon-service.xml)
```

## Resetting a password

```powershell
cd "C:\Program Files\pgmon"
.\pgmon-reset-password.ps1                 # resets 'admin', prompts for the new password
.\pgmon-reset-password.ps1 alice           # resets (or creates, if missing) a specific account
'newpassword' | .\pgmon-reset-password.ps1 admin   # non-interactive, for scripting
```

Must run from an elevated PowerShell prompt (Run as Administrator). If the
named account doesn't exist, it's created fresh as an admin instead of
failing -- so it also covers losing every account, not just forgetting one
password. The Account tab's admin "Reset password..." button builds the
exact piped-non-interactive form of this command for you.

## HTTPS / a different port

There's no reverse proxy in front of Uvicorn on Windows (see above), so for
HTTPS or a standard port 80/443 you have two reasonable options:
- Put IIS or another reverse proxy in front of `http://localhost:8080/`
  yourself, with its own TLS cert.
- Edit `C:\Program Files\pgmon\pgmon-service.xml`'s `<arguments>` to add
  `--port 443 --ssl-keyfile ... --ssl-certfile ...` (Uvicorn's own built-in
  TLS support), then `pgmon-service.exe restart`.

## Known limitations of a Windows install

Same two caveats as the Linux standalone install, for the same reasons:

- **Oracle->Postgres full-schema migration** (the `ora2pg`-based path) needs
  `ora2pg` and the Oracle Instant Client, neither of which this installer
  sets up -- the app already detects their absence and greys out that one
  feature; everything else is unaffected.
- **Automated restore of physical/incremental backups through the UI**
  needs pgmon running inside a Kubernetes cluster (it shells out to
  `kubectl`) -- not applicable to a Windows service. Logical
  (`pg_dump`/`pg_restore`) backup and restore work the same as anywhere
  else, as long as a Postgres client matching or newer than the monitored
  server's version is on `PATH`.
- No bundled `pg_dump`/`pg_restore`/`psql` -- install the Postgres client
  tools yourself (e.g. the official Windows installer from
  postgresql.org) and make sure they're on `PATH` if you want backup
  features.

## Uninstalling

Settings -> Apps -> pgmon -> Uninstall (or `msiexec /x pgmon-1.0.0.msi`).
Stops and removes the service and `C:\Program Files\pgmon\`.
`C:\ProgramData\pgmon\` (config + data) is left behind, matching the Linux
installer's default -- delete it yourself if you want a completely clean
removal.

## Building the MSI

WiX (the MSI compiler) only runs on Windows -- confirmed by direct testing
during development, not assumed: every build attempted on a Linux host
fails with `WIX0389: ...is not a relative path` regardless of WiX version,
a known, unresolved upstream limitation
([wixtoolset/issues#7154](https://github.com/wixtoolset/issues/issues/7154)).

**Recommended: GitHub Actions.** Push a `windows-v1.0.0`-style tag, or run
the "Build Windows MSI" workflow manually from the Actions tab with a
version input -- it builds on a `windows-latest` runner and uploads the
`.msi` as a workflow artifact, no Windows machine of your own needed.

**Or build it yourself on any Windows machine** (or VM) with the free WiX
CLI:

```powershell
dotnet tool install --global wix --version 5.0.2
wix extension add WixToolset.UI.wixext/5.0.2
.\packaging\windows\build-msi.ps1 -Version 1.0.0
# -> dist\pgmon-1.0.0.msi
```

(Needs internet access on the *building* machine only, to download the
Python embeddable runtime, pip wheels, and WinSW -- none of that touches
the machine the resulting MSI gets installed on.)
