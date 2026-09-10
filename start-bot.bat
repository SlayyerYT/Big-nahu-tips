@echo off
title Altcoin Long/Short Bot
cd /d "%~dp0"

echo Starting the altcoin long/short bot...
echo Close this window (or press Ctrl+C) to stop it.
echo.

python bot.py

echo.
echo ============================================
echo  The bot stopped. Read the message above.
echo ============================================
pause
