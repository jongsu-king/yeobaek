$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$releaseMatch = [regex]::Match($env:RELEASE_TAG, '^v(?<version>\d+\.\d+\.\d+)(?:-rc\.\d+)?$')
if (-not $releaseMatch.Success) {
    throw 'RELEASE_TAG must be a version such as v0.4.0 or v0.4.0-rc.1.'
}
$releaseVersion = $releaseMatch.Groups['version'].Value
$assetName = "Yeobaek-$releaseVersion-Windows-x64-Setup.exe"
$downloadFolder = Join-Path $env:RUNNER_TEMP 'yeobaek-release'
$fixtureFolder = Join-Path $env:RUNNER_TEMP 'yeobaek-installer-data'
$installation = Join-Path $env:LOCALAPPDATA "Programs\Yeobaek\$releaseVersion"
$smokeScript = Join-Path $PSScriptRoot 'installer_smoke.py'
New-Item -ItemType Directory -Force $downloadFolder | Out-Null

gh release download $env:RELEASE_TAG --repo $env:GITHUB_REPOSITORY `
    --pattern $assetName --pattern SHA256SUMS.txt --dir $downloadFolder
if ($LASTEXITCODE -ne 0) { throw 'Release download failed.' }
$installer = Join-Path $downloadFolder $assetName
$checksum = @(Get-Content (Join-Path $downloadFolder 'SHA256SUMS.txt') | Where-Object { $_.EndsWith("  $assetName") })
if ($checksum.Count -ne 1 -or (Get-FileHash $installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $checksum[0].Substring(0, 64)) {
    throw 'Installer checksum mismatch.'
}

function Install-Yeobaek {
    $setup = Start-Process -FilePath $installer -ArgumentList '/S' -Wait -PassThru
    if ($setup.ExitCode -ne 0) { throw "Installer failed with $($setup.ExitCode)." }
    if (-not (Test-Path (Join-Path $installation 'python\python.exe'))) { throw 'Installed Python is missing.' }
    if (-not (Test-Path (Join-Path $installation 'app\launch.py'))) { throw 'Installed launcher is missing.' }
}

function Uninstall-Yeobaek {
    $uninstaller = Join-Path $installation 'Uninstall.exe'
    $remove = Start-Process -FilePath $uninstaller -ArgumentList '/S' -Wait -PassThru
    if ($remove.ExitCode -ne 0) { throw "Uninstaller failed with $($remove.ExitCode)." }
    $deadline = (Get-Date).AddSeconds(60)
    while ((Test-Path $installation) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (Test-Path $installation) {
        $remainingFiles = @(Get-ChildItem -LiteralPath $installation -File -Recurse -Force)
        $cacheFiles = @($remainingFiles | Where-Object { $_.FullName -match '\\__pycache__\\' -and $_.Extension -eq '.pyc' })
        Write-Output "Remaining installation files: $($remainingFiles.Count), Python cache files: $($cacheFiles.Count)."
        $remainingFiles | Sort-Object { $_.FullName -match '\\__pycache__\\' }, FullName | Select-Object -First 60 | ForEach-Object {
            Write-Output $_.FullName.Substring($installation.Length + 1)
        }
        python -B $smokeScript preserved --data $fixtureFolder
        throw 'Owned installation directory remained after uninstall.'
    }
    python $smokeScript preserved --data $fixtureFolder
    if ($LASTEXITCODE -ne 0) { throw 'External note preservation failed.' }
}

Install-Yeobaek
$bundledPython = Join-Path $installation 'python\python.exe'
& $bundledPython -B -I $smokeScript seed --app (Join-Path $installation 'app') --data $fixtureFolder
if ($LASTEXITCODE -ne 0) { throw 'Installed runtime smoke failed.' }

$env:YEOBAEK_DATA_DIR = $fixtureFolder
$launcher = Join-Path $installation 'app\launch.py'
$backend = Start-Process -FilePath (Join-Path $installation 'python\pythonw.exe') `
    -ArgumentList @('-I', "`"$launcher`"") -PassThru
$client = $null
try {
    $clientPath = Join-Path $installation 'client\flet.exe'
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        $client = Get-Process -Name flet -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $clientPath } | Select-Object -First 1
        if ($null -ne $client -and $client.MainWindowHandle -ne 0 -and $client.MainWindowTitle -match '여백|Yeobaek') { break }
        if ($backend.HasExited) { throw "App backend exited with $($backend.ExitCode)." }
        Start-Sleep -Milliseconds 500
    }
    if ($null -eq $client -or $client.MainWindowHandle -eq 0 -or $client.MainWindowTitle -notmatch '여백|Yeobaek') {
        throw 'The installed app did not display its native window.'
    }
    Write-Output 'Verified the installed native Yeobaek window.'
    if (-not $client.CloseMainWindow() -or -not $client.WaitForExit(30000)) { throw 'The native app did not close normally.' }
    if (-not $backend.WaitForExit(30000)) { throw 'The app backend remained after closing its window.' }
} finally {
    if ($null -ne $client -and -not $client.HasExited) { $client.Kill() }
    if (-not $backend.HasExited) { $backend.Kill($true) }
}

if ([Version]$releaseVersion -ge [Version]'0.4.0') {
    $cacheProbe = @'
import pathlib, py_compile, sys
source = pathlib.Path(sys.argv[1]) / "yeobaek/notes.py"
generated = py_compile.compile(str(source), doraise=True)
assert pathlib.Path(generated).is_file()
print("Created a known legacy Python cache file for targeted uninstall verification.")
'@
    & $bundledPython -B -I -c $cacheProbe (Join-Path $installation 'app')
    if ($LASTEXITCODE -ne 0) { throw 'Legacy cache probe failed.' }
}

Uninstall-Yeobaek
Install-Yeobaek
& $bundledPython -B -I $smokeScript check --app (Join-Path $installation 'app') --data $fixtureFolder
if ($LASTEXITCODE -ne 0) { throw 'Reinstalled runtime could not recover the existing note.' }
Uninstall-Yeobaek
Write-Output 'Verified Windows install, native launch, normal close, uninstall, reinstall and external note preservation.'
