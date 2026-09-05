; ============================================================
;  LANPrint 服务端安装包脚本（Inno Setup 6）
;  编译: ISCC.exe installer\server.iss （或直接运行 build\build.bat）
;  前置: 先运行 build\build.bat 生成 dist\lanprint_server.exe
;
;  安装流程:
;    1. 复制 lanprint_server.exe 到 {app}
;    2. 写入开机自启动注册表（wscript 隐藏窗口后台运行）
;    3. 安装完成页勾选项依次执行:
;       a) 配置向导（选择共享打印机 / 共享名 / 端口 / 防火墙规则）
;       b) 立即后台启动服务（无窗口）
;  卸载流程:
;    结束进程 -> 删除自启动注册表值 -> 删除防火墙规则 -> 可选删除数据目录
; ============================================================

#define MyAppName "LANPrint 服务端"
#define MyAppNameEn "LANPrint Server"
#define MyAppExeName "lanprint_server.exe"
#define MyAppVersion "1.0.0"
#define FirewallRuleName "LANPrint Server"

[Setup]
AppId={{8F2E7C94-6A3D-4E51-B9F0-C3D8A21E7B46}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=LANPrint
DefaultDirName={autopf}\{#MyAppNameEn}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=lanprint_server_setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; 安装简体中文语言包后可切换中文界面（取消下行注释）:
; Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName} 配置向导"; Filename: "{app}\{#MyAppExeName}"; Parameters: "configure"
Name: "{group}\{#MyAppName} 查看版本"; Filename: "{app}\{#MyAppExeName}"; Parameters: "version"

[Registry]
; 开机自启动：经 wscript 以隐藏窗口运行（后台执行，无控制台窗口）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "LANPrintServer"; \
    ValueData: "wscript.exe ""{app}\start_server_hidden.vbs"""; \
    Flags: uninsdeletevalue

[Run]
; 安装完成页：先配置向导（控制台交互，继承安装器管理员权限以写防火墙规则）
Filename: "{app}\{#MyAppExeName}"; Parameters: "configure"; \
    Description: "运行配置向导（选择共享打印机）"; \
    Flags: postinstall skipifsilent
; 再立即后台启动服务（无窗口）
Filename: "wscript.exe"; Parameters: """{app}\start_server_hidden.vbs"""; \
    Description: "立即后台启动打印服务"; \
    Flags: postinstall nowait runhidden skipifsilent

[UninstallRun]
Filename: "netsh.exe"; \
    Parameters: "advfirewall firewall delete rule name=""{#FirewallRuleName}"""; \
    Flags: runhidden; RunOnceId: "DelFirewallRule"

[Code]
const
  MutexName = 'Global\LANPrintServer';

// 结束正在运行的服务端进程（忽略失败：进程可能不存在）
procedure StopServerProcesses;
var
  ResCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM ' + ExpandConstant('{#MyAppExeName}'),
       '', SW_HIDE, ewWaitUntilTerminated, ResCode);
end;

// 生成后台自启动脚本 start_server_hidden.vbs（{app} 随安装路径替换）
procedure WriteStartVbs;
var
  Content: String;
begin
  Content :=
    ''' LANPrint 服务端后台自启动（无窗口运行）' + #13#10 +
    'CreateObject("WScript.Shell").Run """' +
    ExpandConstant('{app}\{#MyAppExeName}') + '"" run", 0, False' + #13#10;
  SaveStringToFile(ExpandConstant('{app}\start_server_hidden.vbs'),
                   Content, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    StopServerProcesses;               // 覆盖安装前先停掉旧实例
  if CurStep = ssPostInstall then
    WriteStartVbs;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResCode: Integer;
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopServerProcesses;

    // 删除防火墙规则（与 [UninstallRun] 中的规则同名，双保险）
    Exec('netsh.exe',
         'advfirewall firewall delete rule name="{#FirewallRuleName}"',
         '', SW_HIDE, ewWaitUntilTerminated, ResCode);

    // 可选：删除打印缓存与配置数据目录
    DataDir := ExpandConstant('{commonappdata}\LANPrint');
    if DirExists(DataDir) then
      if MsgBox('是否同时删除打印缓存与配置数据？' + #13#10 + DataDir,
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
