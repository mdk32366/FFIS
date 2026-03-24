@echo off
REM Start the Flat File Scrubber Streamlit app
REM Usage: run.bat

cd /d "%~dp0"

REM Activate virtual environment if not already active
if not defined VIRTUAL_ENV (
    call .venv\Scripts\activate.bat
)

REM Start the app
python -m streamlit run flat_file_scrubber.py
