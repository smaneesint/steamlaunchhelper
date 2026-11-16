@echo off
setlocal
cd /d "%~dp0"
if not defined SLH_PYTHON set "SLH_PYTHON=python"
"%SLH_PYTHON%" build_v3_6_1.py %*
exit /b %errorlevel%
