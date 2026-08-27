param(
    [switch]$OneFile,
    [switch]$Installer,
    [ValidatePattern('^[0-9]+(?:\.[0-9]+){2}(?:[-.][A-Za-z0-9.]+)?$')]
    [string]$Version = '0.1.0'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$buildVenv = Join-Path $projectRoot '.build-venv'

if ($OneFile -and $Installer) {
    throw 'The installer requires the default one-folder payload; omit -OneFile.'
}

if (-not (Test-Path -LiteralPath $buildVenv)) {
    $projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $projectPython)) {
        throw 'Run .\scripts\setup.ps1 before building the Windows package.'
    }
    & $projectPython -m venv $buildVenv
}

$buildPython = Join-Path $buildVenv 'Scripts\python.exe'
$pyInstaller = Join-Path $buildVenv 'Scripts\pyinstaller.exe'
& $buildPython -m pip install --disable-pip-version-check --require-hashes -r (
    Join-Path $projectRoot 'requirements\build.lock'
)
if ($LASTEXITCODE -ne 0) {
    throw 'Packaging dependencies could not be installed.'
}

$niceguiSource = Join-Path $buildVenv 'Lib\site-packages\nicegui'
$stylesSource = Join-Path $projectRoot 'finance_app\styles'
$assetsSource = Join-Path $projectRoot 'finance_app\assets'
$niceguiArgument = "$niceguiSource;nicegui"
$stylesArgument = "$stylesSource;finance_app\styles"
$assetsArgument = "$assetsSource;finance_app\assets"
$packageMode = if ($OneFile) { '--onefile' } else { '--onedir' }
$specPath = Join-Path $projectRoot 'build\pyinstaller-spec'
New-Item -ItemType Directory -Force -Path $specPath | Out-Null
Push-Location $projectRoot
try {
    & $pyInstaller app.py `
        --name Expensetics `
        $packageMode `
        --console `
        --clean `
        --noconfirm `
        --specpath $specPath `
        --add-data $niceguiArgument `
        --add-data $stylesArgument `
        --add-data $assetsArgument
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

$executable = if ($OneFile) {
    Join-Path $projectRoot 'dist\Expensetics.exe'
} else {
    Join-Path $projectRoot 'dist\Expensetics\Expensetics.exe'
}
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Packaging completed without creating $executable."
}

Write-Host ''
if ($OneFile) {
    Write-Host 'Windows executable created at dist\Expensetics.exe.' -ForegroundColor Green
    Write-Host 'This diagnostic build extracts its runtime on every launch.'
} else {
    Write-Host 'Windows application payload created in dist\Expensetics.' -ForegroundColor Green
    Write-Host 'Use the complete folder for an installer or portable ZIP.'
}

if ($Installer) {
    $isccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    $isccCandidates = @(
        if ($isccCommand) { $isccCommand.Source }
        Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'
        if (${env:ProgramFiles(x86)}) {
            Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
        }
    )
    $iscc = $isccCandidates | Where-Object {
        $_ -and (Test-Path -LiteralPath $_)
    } | Select-Object -First 1
    if (-not $iscc) {
        throw 'Inno Setup 6 was not found. Install it from https://jrsoftware.org/isinfo.php, then rerun with -Installer.'
    }

    $installerScript = Join-Path $PSScriptRoot 'windows-installer.iss'
    $sourceDirectory = Join-Path $projectRoot 'dist\Expensetics'
    $outputDirectory = Join-Path $projectRoot 'dist'
    & $iscc `
        "/DAppVersion=$Version" `
        "/DSourceDir=$sourceDirectory" `
        "/DOutputDir=$outputDirectory" `
        $installerScript
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE."
    }
    $installer = Join-Path $outputDirectory "Expensetics-Setup-$Version.exe"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "Installer compilation completed without creating $installer."
    }
    Write-Host ''
    Write-Host "Windows installer created at dist\Expensetics-Setup-$Version.exe." -ForegroundColor Green
}
