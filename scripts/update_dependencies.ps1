$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$scriptPath = Join-Path $PSScriptRoot 'update_dependencies.py'

Push-Location $projectRoot
try {
    if (Test-Path -LiteralPath $projectPython) {
        & $projectPython $scriptPath @args
    } else {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if ($python) {
            & $python.Source $scriptPath @args
        } else {
            $launcher = Get-Command py -ErrorAction SilentlyContinue
            if (-not $launcher) {
                throw 'Python 3.11 or newer is required.'
            }
            & $launcher.Source -3 $scriptPath @args
        }
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency maintenance failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
