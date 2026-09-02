@echo off
echo Installing ccBar...

REM 复制到 Program Files
mkdir "C:\Program Files\ccBar" 2>nul
copy /Y ccBar.exe "C:\Program Files\ccBar\"

REM 创建快捷方式到启动文件夹
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
echo Set oWS = WScript.CreateObject("WScript.Shell") > CreateShortcut.vbs
echo sLinkFile = "%STARTUP%\ccBar.lnk" >> CreateShortcut.vbs
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> CreateShortcut.vbs
echo oLink.TargetPath = "C:\Program Files\ccBar\ccBar.exe" >> CreateShortcut.vbs
echo oLink.Save >> CreateShortcut.vbs
cscript CreateShortcut.vbs
del CreateShortcut.vbs

echo ✅ 安装完成！ccBar 将在下次开机时自动启动。
pause
