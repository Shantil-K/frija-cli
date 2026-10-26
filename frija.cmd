@echo off
rem Run frija-cli from this folder on Windows without installing it.
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
where py >nul 2>nul && (py -3 -m frija %*) || (python -m frija %*)
