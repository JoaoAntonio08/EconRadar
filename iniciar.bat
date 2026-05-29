@echo off
chcp 65001 >nul
cd /d "%~dp0backend"

:: Criar venv se não existir
if not exist venv (
    echo [setup] Criando ambiente virtual...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
    echo [setup] Dependencias instaladas.
) else (
    call venv\Scripts\activate.bat
)

echo.
echo      EconRadar Backend iniciando...
echo  Acesse local: http://localhost:8000
echo  Ctrl+C para parar
echo.

:: Inicia o backend em segundo plano
start "EconRadar Backend" python main.py

:: Aguarda o backend subir
timeout /t 3 /nobreak >nul

:: Inicia o ngrok tunelando a porta 8000 (backend + frontend juntos)
echo  Iniciando ngrok na porta 8000...
echo  A URL publica aparecera na janela do ngrok.
echo.
cd /d "%~dp0"
ngrok.exe http 8000

pause
