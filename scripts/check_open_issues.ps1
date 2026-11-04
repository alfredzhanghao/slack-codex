param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $projectRoot '.venv/Scripts/python.exe' }
$runId = [guid]::NewGuid().ToString()
$runDir = Join-Path $projectRoot ".work-issues/verification-$runId"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$previousUtf8 = $env:PYTHONUTF8
$steps = [ordered]@{}
function Invoke-Check([string]$Name, [string[]]$Arguments) {
    & $Python @Arguments *> (Join-Path $runDir "$Name.log")
    $steps[$Name] = ($LASTEXITCODE -eq 0)
    if (-not $steps[$Name]) { throw "Check failed: $Name. See $runDir/$Name.log" }
    Write-Output "$Name passed"
}
Push-Location -LiteralPath $projectRoot
try {
    $env:TEMP = $runDir
    $env:TMP = $runDir
    $env:PYTHONUTF8 = '1'
    Invoke-Check 'pytest' @('-m', 'pytest', '-q', '-p', 'no:cacheprovider',
        "--basetemp=$runDir/pytest", "--junitxml=$runDir/tests.xml")
    Invoke-Check 'ruff' @('-m', 'ruff', 'check', 'src', 'scripts', 'tests', '--no-cache')
    Invoke-Check 'compile' @('-m', 'compileall', '-q', 'src', 'scripts')
    Invoke-Check 'build' @('-m', 'build', '--no-isolation', '--outdir', "$runDir/package")
    Invoke-Check 'archives' @('scripts/p4_package_check.py', "$runDir/package", '--output', "$runDir/archives.json")
    $wheel = @(Get-ChildItem -LiteralPath "$runDir/package" -Filter '*.whl')
    if ($wheel.Count -ne 1) { throw 'Expected exactly one candidate wheel' }
    Invoke-Check 'offline-install' @('-m', 'pip', 'install', '--no-index', '--no-deps', '--no-compile',
        '--disable-pip-version-check', '--target', "$runDir/installed", $wheel[0].FullName)
    $smoke = 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import slack_codex_bridge as b; from slack_codex_bridge import supervisor, attachments, slack; assert Path(b.__file__).is_relative_to(Path(sys.argv[1])); print(b.__version__)'
    Invoke-Check 'installed-imports' @('-I', '-c', $smoke, "$runDir/installed")
    [xml]$junit = Get-Content -LiteralPath "$runDir/tests.xml" -Raw
    $result = [ordered]@{
        status = 'passed'; run_id = $runId; steps = $steps;
        tests = [int]$junit.testsuites.testsuite.tests;
        failures = [int]$junit.testsuites.testsuite.failures;
        errors = [int]$junit.testsuites.testsuite.errors;
        skipped = [int]$junit.testsuites.testsuite.skipped;
        live_slack_acceptance_claimed = $false;
        production_deployment_performed = $false
    }
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$runDir/result.json" -Encoding utf8
    Write-Output "Verification result: $runDir/result.json"
} finally {
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    $env:PYTHONUTF8 = $previousUtf8
    Pop-Location
}
