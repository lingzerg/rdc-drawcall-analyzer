@echo off
chcp 65001 >nul
setlocal

set "ROOT=%~dp0"
set "SCRIPT=%ROOT%analyzer\mobile_rdc_batch_analyze.py"
set "PY=%ROOT%runtime\python\python.exe"
set "BUNDLED_RD=%ROOT%third_party\renderdoc\renderdoccmd.exe"

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

if not exist "%SCRIPT%" (
  echo [ERROR] Missing analyzer script:
  echo   %SCRIPT%
  echo Please re-download or re-clone this repository.
  pause
  exit /b 1
)

if exist "%ROOT%runtime\python\python.exe" (
  echo Python runtime: bundled
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python runtime was not found.
    echo.
    echo This repository normally includes:
    echo   runtime\python\python.exe
    echo.
    echo Fix options:
    echo   1. Re-download/re-clone the full repository, including runtime\python.
    echo   2. Or install Python 3.10+ and make sure python is available in PATH.
    echo.
    echo Python download:
    echo   https://www.python.org/downloads/windows/
    pause
    exit /b 1
  )
  echo Python runtime: system PATH
)

if exist "%BUNDLED_RD%" (
  echo RenderDoc runtime: bundled
) else if exist "C:\Program Files\RenderDoc\renderdoccmd.exe" (
  echo RenderDoc runtime: installed
) else (
  where renderdoccmd.exe >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] RenderDoc command-line runtime was not found.
    echo.
    echo This repository normally includes:
    echo   third_party\renderdoc\renderdoccmd.exe
    echo.
    echo Fix options:
    echo   1. Re-download/re-clone the full repository, including third_party\renderdoc.
    echo   2. Or install RenderDoc and make sure renderdoccmd.exe is available.
    echo.
    echo RenderDoc download:
    echo   https://renderdoc.org/
    pause
    exit /b 1
  )
  echo RenderDoc runtime: system PATH
)
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
