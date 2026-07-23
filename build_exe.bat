@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  Gera dist\PerfilElevacao.exe (Windows, sem console).
REM  Fonte unica do build = PerfilElevacao.spec
REM  Rode UMA VEZ na maquina de desenvolvimento (que tem Python).
REM  O .exe resultante roda em maquinas SEM Python.
REM ============================================================

REM  USAR_UPX=1 comprime as DLLs com UPX -> .exe menor (recomendado).
REM  Se o antivirus reclamar do .exe comprimido, troque para 0 e gere de novo.
set "USAR_UPX=1"
set "UPX_VER=4.2.4"
set "UPX_DIR=%~dp0upx"

echo.
echo [1/4] Instalando dependencias (pandas, pyproj, matplotlib, pyinstaller)...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo.
    echo ERRO: falha ao instalar dependencias. Verifique se o Python esta instalado.
    pause
    exit /b 1
)

set "UPX_OK="
echo.
if "%USAR_UPX%"=="1" (
    echo [2/4] Verificando o compactador UPX...
    where upx >nul 2>&1
    if !errorlevel! == 0 (
        echo    UPX encontrado no PATH.
        set "UPX_OK=path"
    ) else if exist "%UPX_DIR%\upx.exe" (
        echo    UPX encontrado em "%UPX_DIR%".
        set "UPX_OK=dir"
    ) else (
        echo    UPX nao encontrado. Tentando baixar a versao portatil %UPX_VER%...
        powershell -NoProfile -ExecutionPolicy Bypass -Command ^
          "$ErrorActionPreference='Stop';" ^
          "try {" ^
          "  $u='https://github.com/upx/upx/releases/download/v%UPX_VER%/upx-%UPX_VER%-win64.zip';" ^
          "  $z=Join-Path $env:TEMP 'upx_dl.zip';" ^
          "  Invoke-WebRequest -Uri $u -OutFile $z;" ^
          "  $t=Join-Path $env:TEMP 'upx_dl';" ^
          "  if(Test-Path $t){Remove-Item $t -Recurse -Force};" ^
          "  Expand-Archive $z $t -Force;" ^
          "  $e=Get-ChildItem $t -Recurse -Filter upx.exe | Select-Object -First 1;" ^
          "  New-Item -ItemType Directory -Force -Path '%UPX_DIR%' | Out-Null;" ^
          "  Copy-Item $e.FullName (Join-Path '%UPX_DIR%' 'upx.exe') -Force;" ^
          "  Write-Host '   UPX baixado com sucesso.'" ^
          "} catch {" ^
          "  Write-Host ('   AVISO: nao deu para baixar o UPX (' + $_.Exception.Message + ').');" ^
          "  Write-Host '   O build segue SEM compressao UPX (o .exe fica um pouco maior).'" ^
          "}"
        if exist "%UPX_DIR%\upx.exe" set "UPX_OK=dir"
    )
) else (
    echo [2/4] UPX desativado ^(USAR_UPX=0^). O .exe ficara maior, porem "limpo" p/ AV.
)

echo.
echo [3/4] Gerando o executavel a partir do PerfilElevacao.spec...
if "%UPX_OK%"=="dir" (
    python -m PyInstaller --noconfirm --clean --upx-dir "%UPX_DIR%" PerfilElevacao.spec
) else (
    python -m PyInstaller --noconfirm --clean PerfilElevacao.spec
)
if errorlevel 1 (
    echo.
    echo ERRO: falha ao gerar o executavel.
    pause
    exit /b 1
)

echo.
echo [4/4] Pronto!
if exist "dist\PerfilElevacao.exe" (
    for %%F in ("dist\PerfilElevacao.exe") do set /a "MB=%%~zF/1048576"
    echo    dist\PerfilElevacao.exe  ^(~!MB! MB^)
) else (
    echo    AVISO: dist\PerfilElevacao.exe nao foi encontrado.
)
echo    Basta copiar esse arquivo para qualquer Windows (sem Python).
echo.
pause
