@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PORT=8765
if not "%~1"=="" set PORT=%~1

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3.10+ 并加入 PATH。
  echo        下载： https://www.python.org/downloads/
  pause
  exit /b 1
)

echo 正在启动「车票 / 机票 比价搜索」...
echo （首次运行会自动读取内置的基础数据，无需额外配置）
echo.

python -m farecompare serve --port %PORT%
if errorlevel 1 (
  echo.
  echo [提示] 启动失败，可尝试：
  echo         1^) 换端口： 启动.bat 8766
  echo         2^) 检查环境： python -m farecompare check
  pause
)
endlocal
