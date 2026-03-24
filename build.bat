@echo off
REM Script to build and push Flat File Scrubber to Docker registry
REM Usage: build.bat [registry] [tag]
REM Example: build.bat gcr.io/my-project latest

setlocal enabledelayedexpansion
set REGISTRY=%1
set TAG=%2

if "%REGISTRY%"=="" set REGISTRY=flat-file-scrubber
if "%TAG%"=="" set TAG=latest

set IMAGE=%REGISTRY%:%TAG%

echo Building Docker image: %IMAGE%
docker build -t %IMAGE% .

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ✓ Image built successfully!
    echo.
    echo To push to a registry:
    echo   docker push %IMAGE%
    echo.
    echo To run locally:
    echo   docker-compose up -d
    echo.
    echo To test the image:
    echo   docker run -p 8501:8501 %IMAGE%
) else (
    echo ✗ Build failed
    exit /b 1
)
