@echo off
chcp 65001 >nul
setlocal

set "ROOT=%~dp0"
set "SCRIPT=%ROOT%analyzer\mobile_rdc_batch_analyze.py"
set "PY=%ROOT%runtime\python\python.exe"

if not exist "%PY%" (
  set "PY=python"
)

echo ==========================================
echo RenderDoc RDC texture/draw analyzer
echo Auto mode: Vulkan mobile / D3D11 PC
echo ==========================================
echo.
echo Output folder:
echo   %ROOT%analysis_results
echo.

if not "%~1"=="" (
  set "CAPTURE=%~1"
) else (
  echo Drag a .rdc file here, or paste the full file path, then press Enter.
  echo.
  set /p "CAPTURE=RDC path: "
)

if "%CAPTURE%"=="" (
  echo No file path entered.
  pause
  exit /b 1
)

set "CAPTURE=%CAPTURE:"=%"
"%PY%" "%SCRIPT%" "%CAPTURE%"

echo.
pause
