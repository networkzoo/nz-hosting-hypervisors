<#
.SYNOPSIS
  Gets the Network Zoo git repos out of the OneDrive/SharePoint library and into a short local
  path (default C:\dev), avoiding Windows/SharePoint path-length problems and keeping node_modules
  and secrets out of the cloud sync.

.DESCRIPTION
  For each repo in $Repos below, in this order:
    1. already in <DevRoot>\<name>      -> nothing to do (shows git status)
    2. found in the OneDrive library    -> MOVED to <DevRoot>\<name> (files, .git and ignored files
                                           such as .env.local all come along)
    3. otherwise, if a Url is set       -> CLONED from GitHub (secrets like .env.local are NOT in git:
                                           copy them from the other machine yourself)
  Safe to run on both machines and safe to re-run.

.PARAMETER DevRoot      Where repos should live. Default C:\dev
.PARAMETER LegacyRoot   The old OneDrive/SharePoint folder that holds the repos.
.PARAMETER EnableLongPaths  Turn on Windows long paths + git core.longpaths (needs an administrator window).

.EXAMPLE
  .\dev-setup.ps1 -WhatIf        # dry run: shows what it would do, changes nothing
  .\dev-setup.ps1                # do it

.NOTES
  Run it from a neutral folder (e.g. Desktop), not from inside one of the repos it moves.
  Pause OneDrive/SharePoint sync first. Moving a repo out of the library makes OneDrive delete the
  old copy (and sync that deletion to the other machine); SharePoint keeps deleted items in its
  recycle bin for a while.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$DevRoot = 'C:\dev',
    [string]$LegacyRoot = (Join-Path $env:USERPROFILE 'Network Zoo\Sanctuary - Documents\Claude Projects'),
    [switch]$EnableLongPaths
)
$ErrorActionPreference = 'Stop'

# Name   = folder created under DevRoot
# Legacy = folder name inside LegacyRoot where it used to live
# Url/Branch = used only when it has to be cloned (leave Url empty to skip cloning)
$Repos = @(
    @{ Name = 'doc-manager';            Legacy = 'doc-manager';                  Url = 'https://github.com/networkzoo/doc-manager.git';             Branch = 'main' },
    @{ Name = 'hypervisor-web';          Legacy = 'Scale and Prox hosted page';   Url = 'https://github.com/networkzoo/nz-hosting-hypervisors.git';  Branch = 'claude/optimistic-davinci-65lq7d' },
    @{ Name = 'tools';                  Legacy = 'Tools (tools.networkzoo.ca)';  Url = 'https://github.com/networkzoo/networkzoo-tools.git';       Branch = 'master' }
)

function Write-Step($msg) { Write-Host ">> $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "   WARNING: $msg" -ForegroundColor Yellow }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is not installed or not on PATH. Install Git for Windows first."
}

if ($EnableLongPaths) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
               ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw "-EnableLongPaths needs an administrator PowerShell window." }
    Write-Step "Enabling Windows long paths and git core.longpaths"
    if ($PSCmdlet.ShouldProcess('HKLM LongPathsEnabled', 'set to 1')) {
        New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled' `
            -Value 1 -PropertyType DWORD -Force | Out-Null
        git config --global core.longpaths true
    }
}

if (-not (Test-Path $DevRoot)) {
    Write-Step "Creating $DevRoot"
    if ($PSCmdlet.ShouldProcess($DevRoot, 'create folder')) { New-Item -ItemType Directory -Path $DevRoot | Out-Null }
}

foreach ($r in $Repos) {
    $target = Join-Path $DevRoot $r.Name
    $legacy = Join-Path $LegacyRoot $r.Legacy
    Write-Host ""
    Write-Step "$($r.Name)"

    if (Test-Path (Join-Path $target '.git')) {
        Write-Host "   already at $target"
        git -C $target status --short --branch
        continue
    }

    if (Test-Path (Join-Path $legacy '.git')) {
        # Information only - a move keeps everything, but it's good to know the state.
        $dirty = git -C $legacy status --porcelain 2>$null
        if ($dirty) { Write-Warn "uncommitted changes in $legacy (they will move with it)" }
        $ahead = git -C $legacy log '@{u}..' --oneline 2>$null
        if ($ahead) { Write-Warn "unpushed commits in $legacy (they will move with it)" }

        Write-Host "   moving $legacy  ->  $target"
        if ($PSCmdlet.ShouldProcess("$legacy -> $target", 'move repo')) {
            # robocopy copes with long paths and carries .git and ignored files (e.g. .env.local).
            robocopy $legacy $target /E /MOVE /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "robocopy failed (exit $LASTEXITCODE) moving $legacy" }
            # Remove the now-empty old folder if possible.
            if ((Test-Path $legacy) -and -not (Get-ChildItem -Force -LiteralPath $legacy)) {
                Remove-Item -LiteralPath $legacy -Force
            } elseif (Test-Path $legacy) {
                Write-Warn "$legacy still has files in it (probably node_modules/.next); delete it by hand once you've checked."
            }
            git -C $target status --short --branch
        }
        continue
    }

    if ($r.Url) {
        Write-Host "   not in the library on this machine - cloning $($r.Url)"
        if ($PSCmdlet.ShouldProcess($target, "git clone $($r.Url)")) {
            git clone --branch $r.Branch $r.Url $target
            if ($LASTEXITCODE -ne 0) { throw "git clone failed for $($r.Name)" }
            Write-Warn "Secrets and local config are not in git. Copy any .env / .env.local files from the other machine (not via OneDrive or email), or recreate them from the .env.example files."
        }
        continue
    }

    Write-Warn "not found at $target or $legacy, and no Url set in the script - skipped."
}

Write-Host ""
Write-Step "Done. Next steps"
Write-Host "   * doc-manager: cd $DevRoot\doc-manager ; pnpm install   (needs Node + pnpm)"
Write-Host "   * Resume OneDrive/SharePoint sync, then check the old library folders are gone."
Write-Host "   * Delete %USERPROFILE%\env.local.bak once the app runs from the new location."
