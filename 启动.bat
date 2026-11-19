@echo off
chcp 936 >nul
cd /d "%~dp0"
title 法律文书抓取器

rem ===========================================================================
rem  启动.bat —— 双击我就能打开抓取界面
rem ---------------------------------------------------------------------------
rem  【重要：这个文件的编码不能随便改】
rem    本文件用 GBK(936) 编码保存，开头那句 chcp 936 也是设成 GBK。
rem    两者必须配套 —— 如果只用记事本把它另存成 UTF-8，中文提示就会变乱码。
rem
rem  这个文件做了四件事：
rem    1) chcp 936      把控制台代码页设成简体中文，中文提示才不乱码
rem    2) cd /d "%~dp0"  切到「本文件所在的文件夹」，从哪双击都能找到文件
rem    3) 找一个能用的 Python（先试 py，再试 python）
rem    4) 启动 gui.py
rem ===========================================================================

set PYTHON_CMD=

rem 优先用 py 启动器：Python 官方安装器自带，能避开应用商店的假 python
py -V >nul 2>nul
if %errorlevel%==0 set PYTHON_CMD=py

if not defined PYTHON_CMD (
    python -V >nul 2>nul
    if %errorlevel%==0 set PYTHON_CMD=python
)

if not defined PYTHON_CMD goto NOPYTHON

%PYTHON_CMD% gui.py
if %errorlevel% neq 0 goto FAILED
exit /b 0

:NOPYTHON
echo.
echo   [错误] 在这台电脑上没有找到可用的 Python。
echo.
echo   解决办法：
echo     1. 打开 https://www.python.org/downloads/ 下载 Python 3
echo     2. 安装时务必勾选 "Add Python to PATH"
echo     3. 装完关掉这个窗口，重新双击 启动.bat
echo.
pause
exit /b 1

:FAILED
echo.
echo   程序运行时出错并退出了。请把上面的报错内容截图发给我。
echo.
pause
exit /b 1
