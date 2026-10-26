@echo off
cd /d "%~dp0"
set PY=py -V:3.13
%PY% --version >nul 2>&1 || set PY=py
%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail
%PY% -m PyInstaller --noconfirm --onefile --windowed --name BloodlinesParty --collect-submodules websockets --collect-all winrt PartyUI.py
if errorlevel 1 goto fail
echo.
echo Done! Your app is at: %~dp0dist\BloodlinesParty.exe
echo Send that one file to your party.
pause
exit /b 0
:fail
echo.
echo Build failed - scroll up for the error.
pause
exit /b 1
