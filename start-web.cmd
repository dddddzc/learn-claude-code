@echo off
cd /d "%~dp0web"
call npm install
call npm run dev