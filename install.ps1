$ErrorActionPreference = 'Stop'
$dst = Join-Path $env:LOCALAPPDATA 'ccBar'
New-Item -ItemType Directory -Force -Path $dst | Out-Null

$exe = Join-Path $dst 'ccBar.exe'
$release = 'https://github.com/bmfish/ccbar-win/releases/latest/download/ccBar.exe'
Write-Host "Downloading ccBar.exe..."
Invoke-WebRequest -Uri $release -OutFile $exe -UseBasicParsing

# 只建开始菜单快捷方式；开机自启由应用内设置控制（HKCU\...\Run，
# 见托盘菜单 → 设置 → 开机自动启动），避免和启动文件夹快捷方式重复拉起两份。
$lnkPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'ccBar.lnk'
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $exe
$lnk.Save()

Write-Host "Installed to $dst, Start Menu shortcut created."
Write-Host "如需开机自启，请在 ccBar 设置里勾选「开机自动启动」。"
Start-Process $exe
