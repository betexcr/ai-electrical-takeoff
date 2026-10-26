<#
.SYNOPSIS
  Start the takeoff app for a demo (docs/demo_script.md).

.DESCRIPTION
  Uses its own store, data/takeoff/demo, so a demo never touches the working store data/takeoff.
  With AI on, it first runs the live preflight's cheap checks (credentials, API host, model, one
  vision request; a few cents) and stops if any fails. Then it serves http://127.0.0.1:<Port> and
  opens the browser. Stop the server with Ctrl+C.

.PARAMETER Fresh
  Start an empty demo store. The old one is moved aside to data/takeoff/demo-<time>, never deleted.
.PARAMETER NoAI
  det/0 only; no Anthropic request is ever made.
.PARAMETER IgnoreEnvFile
  Do not load the repo-root .env, so the `ant auth login` profile is used even if .env holds a key.
.PARAMETER SkipCheck
  Skip the preflight.
#>
param(
    [switch]$Fresh,
    [switch]$NoAI,
    [switch]$IgnoreEnvFile,
    [switch]$SkipCheck,
    [int]$Port = 8766
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$backend = Join-Path $root "backend"
$store = Join-Path $root "data\takeoff\demo"
$envFile = Join-Path $root ".env"

function Invoke-Tool([string]$tool, [string[]]$toolArgs) {
    # As a script the tools load .env; called through main() they do not.
    if ($IgnoreEnvFile) {
        $code = "import sys; sys.path.insert(0, 'tools'); import $tool; sys.exit($tool.main(sys.argv[1:]))"
        & uv run python -c $code @toolArgs | Out-Host
    } else {
        & uv run python "tools\$tool.py" @toolArgs | Out-Host
    }
    return $LASTEXITCODE
}

if (-not $NoAI) {
    $envHasKey = (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^\s*(export\s+)?ANTHROPIC_API_KEY\s*=\s*\S' -Quiet)
    $usingEnvKey = $envHasKey -and -not $IgnoreEnvFile -and -not $env:ANTHROPIC_API_KEY
    if ($usingEnvKey) {
        Write-Host "Using ANTHROPIC_API_KEY from the repo-root .env (it takes precedence over an 'ant auth login' profile)."
        Write-Host "  A key that is not scoped to a workspace is rejected by the API; -IgnoreEnvFile uses your ant login instead."
    }
    if ($IgnoreEnvFile -and (Test-Path Env:ANTHROPIC_API_KEY) -and -not $env:ANTHROPIC_API_KEY) {
        Remove-Item Env:ANTHROPIC_API_KEY   # an empty key would still be "set"; let the SDK find the profile
    }
}

if ($Fresh -and (Test-Path $store)) {
    $aside = "$store-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Move-Item $store $aside
    Write-Host "Previous demo store moved to $aside"
}
New-Item -ItemType Directory -Force $store | Out-Null

Push-Location $backend
try {
    if (-not $NoAI -and -not $SkipCheck) {
        Write-Host "Checking AI access (credentials, host, model, one vision request)..."
        $rc = Invoke-Tool "ai_preflight" @("--no-sheet-run", "--out", (Join-Path $store "ai_preflight.json"))
        if ($rc -ne 0) {
            Write-Host "AI check failed: see $store\ai_preflight.json." -ForegroundColor Red
            if ($usingEnvKey) { Write-Host "  The .env key was used. Fix it, or run .\demo.ps1 -IgnoreEnvFile to use your ant login." }
            Write-Host "  Otherwise: 'ant auth login' and run again, or .\demo.ps1 -NoAI for det/0 only."
            exit 1
        }
    }

    $url = "http://127.0.0.1:$Port/"
    Start-Job -ScriptBlock {
        param($u)
        for ($i = 0; $i -lt 120; $i++) {
            try { Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec 2 | Out-Null; Start-Process $u; return } catch { Start-Sleep -Milliseconds 500 }
        }
    } -ArgumentList $url | Out-Null

    $serveArgs = @("serve", "--data", $store, "--port", "$Port", "--ai", $(if ($NoAI) { "off" } else { "on" }))
    Write-Host "Serving $url  (store: $store; AI $(if ($NoAI) { 'off' } else { 'on' })). Ctrl+C to stop."
    $null = Invoke-Tool "takeoff_app" $serveArgs
} finally {
    Pop-Location
    Get-Job | Remove-Job -Force -ErrorAction SilentlyContinue
}
