@echo off
REM ---------------------------------------------------------------
REM  Construit PCMonitor-Agent.exe et prépare le dossier site\
REM  Usage :  build_agent.bat https://monitor.votredomaine.com
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage : build_agent.bat https://monitor.votredomaine.com
  exit /b 1
)
set SITE_URL=%~1

echo [1/4] Configuration de l'agent pour %SITE_URL%
> agent\agent_config.py echo SITE_URL = "%SITE_URL%"

echo [2/4] Dependances
python -m pip install --quiet --upgrade psutil pyinstaller || exit /b 1

echo [3/4] Compilation de l'executable
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name PCMonitor-Agent ^
  --add-data "%~dp0agent\dashboard.html;." ^
  --paths "%~dp0agent" --hidden-import agent_config ^
  --distpath "%~dp0build\dist" --workpath "%~dp0build\work" --specpath "%~dp0build" ^
  "%~dp0agent\server.py" || exit /b 1

echo [4/4] Preparation du site
if not exist site\download mkdir site\download
copy /y build\dist\PCMonitor-Agent.exe site\download\PCMonitor-Agent.exe >nul
copy /y agent\dashboard.html site\index.html >nul

echo.
echo Termine : le dossier "site" est pret a etre envoye sur le VPS.
endlocal
