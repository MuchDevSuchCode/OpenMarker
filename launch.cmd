@echo off
rem Start OpenMarker. The first run creates a private Python environment in
rem .venv and installs OpenMarker into it; later runs start straight away.
rem
rem   launch                 open the library in your browser
rem   launch --with-ai       also install the AI picture cut-out, then open
rem   launch <command> ...   run any OpenMarker command, e.g. launch setup
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"

set "TARGET=."
if /i "%~1"=="--with-ai" set "TARGET=.[ai]"

if not exist "%PY%" (
    echo Setting up OpenMarker. This happens once and takes a few minutes.
    python -m venv "%ROOT%.venv" || goto :failed
    set "INSTALL=1"
)
if /i "%~1"=="--with-ai" set "INSTALL=1"
if defined INSTALL (
    pushd "%ROOT%"
    "%PY%" -m pip install --quiet --disable-pip-version-check -e "%TARGET%" || (popd & goto :failed)
    popd
)

if "%~1"=="" goto :ui
if /i "%~1"=="--with-ai" goto :ui
"%PY%" -m openmarker %*
exit /b %errorlevel%

:ui
"%PY%" -m openmarker ui
exit /b %errorlevel%

:failed
echo.
echo Setup failed. OpenMarker needs Python 3.12 or newer on the PATH.
pause
exit /b 1
