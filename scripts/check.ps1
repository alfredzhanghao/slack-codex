$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
try {
    $env:TEMP = Join-Path $projectRoot '.work-p1/temp'
    $env:TMP = $env:TEMP
    New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
    $projectPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
    & $projectPython -m pytest -q --basetemp=.work-p1/pytest --junitxml=.work-p1/pytest-results.xml
    if ($LASTEXITCODE -ne 0) { throw 'pytest failed' }
    & $projectPython -m ruff check src tests
    if ($LASTEXITCODE -ne 0) { throw 'ruff failed' }
    & $projectPython -m compileall -q src
    if ($LASTEXITCODE -ne 0) { throw 'compileall failed' }
    & $projectPython -m build --no-isolation
    if ($LASTEXITCODE -ne 0) { throw 'package build failed' }
} finally {
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    Pop-Location
}
