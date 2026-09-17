$ErrorActionPreference = 'Stop'
$dst = Join-Path $env:LOCALAPPDATA 'ccBar'
New-Item -ItemType Directory -Force -Path $dst | Out-Null

$exe = Join-Path $dst 'ccBar.exe'
$release = 'https://github.com/bmfish/ccbar-win/releases/latest/download/ccBar.exe'
Write-Host "Downloading ccBar.exe..."
Invoke-WebRequest -Uri $release -OutFile $exe -UseBasicParsing

$lnkPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'ccBar.lnk'
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $exe
$lnk.Save()

Write-Host "Installed to $dst, startup shortcut created."
Start-Process $exe
