@echo off
echo Building ccBar...

REM 安装依赖
pip install -r requirements.txt
pip install pyinstaller

REM 打包
pyinstaller --onefile --noconsole --name ccBar main.py

echo Done! Output: dist/ccBar.exe
pause
