#!/usr/bin/env pwsh
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CommandArgs)

$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$scriptRoot = Split-Path -Parent $PSCommandPath

$candidates = @()
if ($env:WECHAT_ARTICLE_PYTHON) {
    $candidates += [pscustomobject]@{ Source = $env:WECHAT_ARTICLE_PYTHON; Prefix = @() }
} else {
    $stateHome = if ($env:WECHAT_ARTICLE_HOME) {
        $expanded = [Environment]::ExpandEnvironmentVariables($env:WECHAT_ARTICLE_HOME)
        if ($expanded -eq "~") {
            $expanded = $env:USERPROFILE
        } elseif ($expanded.StartsWith("~\") -or $expanded.StartsWith("~/")) {
            $expanded = Join-Path $env:USERPROFILE $expanded.Substring(2)
        }
        [IO.Path]::GetFullPath($expanded)
    } else {
        $appDataRoot = if ($env:APPDATA) { $env:APPDATA } else { $env:LOCALAPPDATA }
        if ($appDataRoot) { Join-Path $appDataRoot "wechat-article-link-reviewer" } else { "" }
    }
    if ($stateHome) {
        $venvPython = Join-Path $stateHome "venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
            $candidates += [pscustomobject]@{ Source = $venvPython; Prefix = @() }
        }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { $candidates += [pscustomobject]@{ Source = $python.Source; Prefix = @() } }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { $candidates += [pscustomobject]@{ Source = $launcher.Source; Prefix = @("-3") } }
}

foreach ($candidate in $candidates) {
    $versionText = & $candidate.Source @($candidate.Prefix) -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    try {
        if ([version]$versionText -lt [version]"3.10") { continue }
    } catch {
        continue
    }
    & $candidate.Source @($candidate.Prefix) -c "import bs4, curl_cffi, requests" 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    & $candidate.Source @($candidate.Prefix) (Join-Path $scriptRoot "runtime.py") @CommandArgs
    exit $LASTEXITCODE
}

throw "Python 3.10+ with curl_cffi, requests, and beautifulsoup4 is required; run the installer or set WECHAT_ARTICLE_PYTHON"
