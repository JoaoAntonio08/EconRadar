@echo off
chcp 65001 >nul
cd /d "%~dp0backend"

:: Criar venv se não existir
if not exist venv (
    echo [setup] Criando ambiente virtual...
    python -m venv venv
    call venv\Scripts\activate.bat
) else (
    call venv\Scripts\activate.bat
)

:: Sempre sincroniza as dependencias (rapido se ja estiverem instaladas;
:: garante que bibliotecas novas do banco de dados sejam instaladas mesmo
:: numa venv que ja existia de uma versao anterior)
echo [setup] Verificando dependencias...
pip install --upgrade pip -q
pip install -r requirements.txt -q

:: Auto-setup do banco: gera chaves que faltarem no .env e cria as tabelas
:: na primeira vez (nao precisa rodar SQL na mao)
python bootstrap.py
if errorlevel 1 (
    echo.
    echo O EconRadar nao conseguiu iniciar. Veja o erro acima.
    pause
    exit /b 1
)

echo.
echo      EconRadar Backend iniciando...
echo  Acesse local: http://localhost:8000
echo  Ctrl+C para parar
echo.

:: Inicia o backend em segundo plano (janela fica aberta mesmo se crashar,
:: para dar tempo de ler o erro em vez de fechar sozinha)
start "EconRadar Backend" cmd /k "call venv\Scripts\activate.bat && python main.py"

:: Aguarda o backend subir
timeout /t 3 /nobreak >nul

:: Inicia o ngrok tunelando a porta 8000 (backend + frontend juntos)
echo  Iniciando ngrok na porta 8000...
echo  A URL publica aparecera na janela do ngrok.
echo.
cd /d "%~dp0"
ngrok.exe http 8000

pause
