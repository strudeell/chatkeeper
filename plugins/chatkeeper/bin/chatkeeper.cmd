@echo off
rem Chatkeeper launcher for Windows.
rem
rem ASCII only, on purpose: cmd.exe reads batch files in the OEM codepage,
rem so any UTF-8 Cyrillic here breaks into garbage and the interpreter
rem tries to run the fragments as commands. Human-readable messages in
rem Russian come from the python scripts, where encoding is under control.
rem
rem Arguments are re-quoted one by one with shift. The earlier `for /f` version
rem died on any argument containing & | > ^ - and meeting titles come straight
rem from chat messages, so "Acme & Partners" is an ordinary case, not an edge one.
rem No delayed expansion here either: it would eat exclamation marks in titles.
rem
rem Usage: chatkeeper <module> [args]
rem   chatkeeper setup check
rem   chatkeeper collect login ^| chats ^| fetch ^| done
rem   chatkeeper digest preview ^| send
setlocal

set "PLUGIN_DIR=%~dp0.."
if defined CLAUDE_PLUGIN_DATA (
  set "DATA=%CLAUDE_PLUGIN_DATA%"
) else (
  set "DATA=%USERPROFILE%\.claude\plugins\data\chatkeeper-dev"
)
set "PY=%DATA%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  rem Addressed to Claude, not to the human: they never see this line, and without
  rem an explicit pointer Claude used to close the loop - advising to start the
  rem installation that begins with this very command.
  echo ERROR: python environment is not ready ^(%PY%^).
  echo Setup was never completed. Claude: read "%PLUGIN_DIR%\install.md"
  echo and run the installation from step 1 - it creates the environment.
  exit /b 1
)

if "%~1"=="" (
  echo Usage: chatkeeper ^<setup^|collect^|digest^|send^|voice^> [args]
  exit /b 2
)

set "MODULE=%~1"
shift

set ARGS=
:collect_args
if "%~1"=="" goto run
set ARGS=%ARGS% "%~1"
shift
goto collect_args

:run
"%PY%" "%PLUGIN_DIR%\scripts\%MODULE%.py"%ARGS%
exit /b %ERRORLEVEL%
