$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot '.venv'
$requirementsPath = Join-Path $projectRoot 'requirements\runtime.lock'
$markerPath = Join-Path $venvPath '.requirements.sha256'
$requiredHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirementsPath).Hash
$venvPython = Join-Path $venvPath 'Scripts\python.exe'

function Test-SupportedPython([string]$Executable, [string[]]$Arguments = @()) {
    & $Executable @Arguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    return $LASTEXITCODE -eq 0
}

if ((Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $markerPath)) {
    $installedHash = (Get-Content -LiteralPath $markerPath -Raw).Trim()
    if ($installedHash -eq $requiredHash -and (Test-SupportedPython $venvPython)) {
        & $venvPython -c "import nicegui, sqlcipher3" 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host 'Expensetics environment is ready.' -ForegroundColor Green
            return
        }
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    if (Test-Path -LiteralPath $venvPath) {
        throw 'The existing .venv is incomplete. Remove that project-local folder, then run setup again.'
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and (Test-SupportedPython $python.Source)) {
        & $python.Source -m venv $venvPath
    } else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher -and (Test-SupportedPython $launcher.Source @('-3'))) {
            & $launcher.Source -3 -m venv $venvPath
        } else {
            throw 'Python 3.11 or newer is required. Install Python, then run this script again.'
        }
    }
}

if (-not (Test-SupportedPython $venvPython)) {
    throw 'The existing .venv uses Python older than 3.11. Recreate that project-local environment with Python 3.11 or newer.'
}

& $venvPython -m pip install --disable-pip-version-check --require-hashes -r $requirementsPath
if ($LASTEXITCODE -ne 0) {
    throw 'Dependency installation did not complete.'
}
Set-Content -LiteralPath $markerPath -Value $requiredHash -Encoding ascii

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host "Run: .\.venv\Scripts\python.exe app.py"
