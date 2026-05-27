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
echo 
echo      EconRadar Backend iniciando...
echo  Acesse: http://localhost:8000
echo  Docs:   http://localhost:8000/docs 
echo  Ctrl+C para parar                 
echo.

python main.py
pause
