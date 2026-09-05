@echo off
rem ============================================================
rem  LANPrint 打包脚本
rem  用法: build.bat [server|client|all]  （默认 all）
rem  产物:
rem    dist\lanprint_server.exe          (PyInstaller 单文件)
rem    dist\lanprint_server_setup.exe    (Inno Setup 安装包，需已安装 Inno Setup 6)
rem    dist\lanprint_client.exe          (PyInstaller 单文件)
rem    dist\lanprint_client_setup.exe    (Inno Setup 安装包，需已安装 Inno Setup 6)
rem ============================================================
setlocal
cd /d "%~dp0.."

set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=all"

rem 沙箱/隔离环境: 若项目内 .pydeps 存在则优先使用其中的 PyInstaller
if exist "%~dp0..\.pydeps" set "PYTHONPATH=%~dp0..\.pydeps"

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"

if /i "%TARGET%"=="server" goto :server
if /i "%TARGET%"=="client" goto :client
if /i "%TARGET%"=="all" goto :server
echo [错误] 未知目标: %TARGET%  (可用: server ^| client ^| all)
exit /b 1

:server
echo [server 1/2] PyInstaller 打包...
python -m PyInstaller --clean --noconfirm build\server.spec
if errorlevel 1 (
    echo [错误] PyInstaller 打包失败
    exit /b 1
)
if not exist "dist\lanprint_server.exe" (
    echo [错误] 未找到 dist\lanprint_server.exe
    exit /b 1
)

echo.
echo [server 2/2] Inno Setup 编译安装包...
if exist "%ISCC%" (
    "%ISCC%" installer\server.iss
    if errorlevel 1 (
        echo [错误] Inno Setup 编译失败
        exit /b 1
    )
) else (
    echo [提示] 未找到 Inno Setup 6 ^(https://jrsoftware.org/isinfo.php^)，
    echo         已跳过安装包编译。安装后重新运行本脚本即可生成
    echo         dist\lanprint_server_setup.exe。
)
if /i "%TARGET%"=="server" goto :done

:client
echo.
echo [client 1/2] PyInstaller 打包...
python -m PyInstaller --clean --noconfirm build\client.spec
if errorlevel 1 (
    echo [错误] PyInstaller 打包失败
    exit /b 1
)
if not exist "dist\lanprint_client.exe" (
    echo [错误] 未找到 dist\lanprint_client.exe
    exit /b 1
)

echo.
echo [client 2/2] Inno Setup 编译安装包...
if exist "%ISCC%" (
    "%ISCC%" installer\client.iss
    if errorlevel 1 (
        echo [错误] Inno Setup 编译失败
        exit /b 1
    )
) else (
    echo [提示] 未找到 Inno Setup 6，已跳过安装包编译。
)

:done
echo.
echo [完成] 构建产物位于 dist\ 目录
endlocal
