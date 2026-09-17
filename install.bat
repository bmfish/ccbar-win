@echo off
echo Installing ccBar...

set TARGET=%LOCALAPPDATA%\ccBar
mkdir "%TARGET%" 2>nul
copy /Y "%~dp0ccBar.exe" "%TARGET%\"

echo Set oWS = WScript.CreateObject("WScript.Shell") > "%TEMP%\_ccbar_sc.vbs"
echo sLinkFile = oWS.SpecialFolders("Startup") ^& "\ccBar.lnk" >> "%TEMP%\_ccbar_sc.vbs"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%TEMP%\_ccbar_sc.vbs"
echo oLink.TargetPath = "%TARGET%\ccBar.exe" >> "%TEMP%\_ccbar_sc.vbs"
echo oLink.Save >> "%TEMP%\_ccbar_sc.vbs"
cscript //nologo "%TEMP%\_ccbar_sc.vbs"
del "%TEMP%\_ccbar_sc.vbs"

echo Done! ccBar will start automatically on next login.
pause
