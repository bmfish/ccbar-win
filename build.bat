@echo off
echo Building ccBar...
cd /d "%~dp0"

REM ============================================================
REM 前置校验：源文件完整性（历史事故：源文件被清零为 0 字节导致启动崩溃）
REM ============================================================
echo [1/3] 校验源文件完整性...
set ERR=0
for %%f in (main.py ui_kit.py trae_sync.py stats_store.py l10n.py weekly_report.py themes.py share_card.py qr.py autostart.py app_settings.py update_check.py) do (
    if not exist "%%f" (
        echo   [FAIL] 缺少源文件: %%f
        set ERR=1
    ) else if "%%~zf"=="0" (
        echo   [FAIL] 源文件为 0 字节: %%f
        set ERR=1
    )
)
if %ERR%==1 (
    echo 源文件校验未通过，请先 git checkout 恢复源码后再打包。
    pause
    exit /b 1
)
echo   全部 %~dp0*.py 源文件非空，校验通过。

REM ============================================================
REM 冒烟测试：核心模块可导入（import 失败 = 启动即崩溃）
REM ============================================================
echo [2/3] import 冒烟测试...
python -c "import main, ui_kit, trae_sync, stats_store, l10n, themes, qr" 2>nul
if errorlevel 1 (
    echo   [FAIL] 核心模块 import 失败，请检查报错后再打包。
    pause
    exit /b 1
)
echo   核心模块 import 通过。

REM ============================================================
REM 安装依赖（pyinstaller 已存在则跳过，避免重复下载）
REM ============================================================
echo [3/3] 安装依赖...
pip install -r requirements.txt
python -c "import PyInstaller" 2>nul || pip install pyinstaller

REM 打包
pyinstaller --onefile --noconsole --name ccBar --icon ccBar.ico --add-data "ccBar.ico;." main.py

echo Done! Output: dist/ccBar.exe
pause
