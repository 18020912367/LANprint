; ============================================================
;  LANPrint 客户端安装包脚本（Inno Setup 6）
;  编译: ISCC.exe installer\client.iss （或直接运行 build\build.bat client）
;  前置: 先运行 build\build.bat client 生成 dist\lanprint_client.exe
;
;  安装流程:
;    1. 复制 lanprint_client.exe 到 {app}
;    2. 向导页收集: 服务端 IP / 端口 / 共享名（已注册过则自动预填）
;    3. PrepareToInstall: check-server 连通校验（不通则中止安装）
;    4. 写入开机自启动注册表（wscript 隐藏窗口运行 agent）
;    5. ssPostInstall 阶段自动执行完整注册（管理员上下文，无需打勾）
;    6. 安装完成页可选: 立即启动后台代理 agent（无窗口，托盘运行）
;  卸载流程:
;    结束进程 -> unregister 删除打印机与端口 -> 删除自启 -> 可选删除数据目录
; ============================================================

#define MyAppName "LANPrint 客户端"
#define MyAppNameEn "LANPrint Client"
#define MyAppExeName "lanprint_client.exe"
#define MyAppVersion "1.0.6"

[Setup]
AppId={{B7D41A62-9C53-4F2E-A1D8-6E4B2C9F0A75}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=LANPrint
DefaultDirName={autopf}\{#MyAppNameEn}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=lanprint_client_setup
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
; 连通性预检副本：dontcopy 不真正安装，仅供 PrepareToInstall 在文件复制前
; 解压到 {tmp} 运行（{app} 中的 exe 此时尚未就位，不能直接引用）
Source: "..\dist\{#MyAppExeName}"; DestDir: "{tmp}"; Flags: dontcopy

[Registry]
; 开机自启动：经 wscript 以隐藏窗口运行 agent（托盘后台，无控制台窗口）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "LANPrintClient"; \
    ValueData: "wscript.exe ""{app}\start_agent_hidden.vbs"""; \
    Flags: uninsdeletevalue

[Icons]
; 开始菜单入口：手动快速启动后台代理（agent 未自启/被结束时的恢复手段）
Name: "{group}\LANPrint 后台代理"; Filename: "wscript.exe"; \
    Parameters: """{app}\start_agent_hidden.vbs"""; \
    IconFilename: "{app}\{#MyAppExeName}"; \
    Comment: "手动启动 LANPrint 后台代理（托盘状态与通知）"

[Run]
; 后台启动常驻代理（无窗口，托盘状态与通知）——注册已在 ssPostInstall 自动完成
Filename: "wscript.exe"; Parameters: """{app}\start_agent_hidden.vbs"""; \
    Description: "启动后台代理（托盘状态与通知）"; \
    Flags: postinstall nowait runhidden skipifsilent

[Code]
const
  MutexName = 'Local\LANPrintAgent';

var
  ServerPage: TInputQueryWizardPage;

// 从客户端已保存配置 client.json 读取字段值（用于向导页预填/升级免重复输入）
// 文件为 UTF-8 JSON（每键一行）；仅需匹配 ASCII 键与引号包裹的值
function GetSavedServerValue(const Key: String): String;
var
  Lines: TArrayOfString;
  I, P, V: Integer;
  KeyStart, Tail: String;
begin
  Result := '';
  if not FileExists(ExpandConstant('{commonappdata}\LANPrint\client.json')) then
    Exit;
  LoadStringsFromFile(ExpandConstant('{commonappdata}\LANPrint\client.json'),
                      Lines);
  KeyStart := '"' + Key + '"';
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    P := Pos(KeyStart, Lines[I]);
    if P > 0 then
    begin
      Tail := Copy(Lines[I], P + Length(KeyStart), MaxInt);
      P := Pos('"', Tail);                 // 值开始引号
      if P = 0 then Exit;
      Tail := Copy(Tail, P + 1, MaxInt);
      V := Pos('"', Tail);                 // 值结束引号
      if V = 0 then Exit;
      Result := Copy(Tail, 1, V - 1);
      Exit;
    end;
  end;
end;

// 服务端连接参数向导页（IP / 端口 / 共享名）
procedure InitializeWizard;
var
  Saved: String;
begin
  ServerPage := CreateInputQueryPage(wpSelectDir,
    'LANPrint 服务端信息', '请填写要连接的打印服务端',
    '填写局域网内 LANPrint 服务端的信息（IP 地址、端口与共享打印机名）。');
  ServerPage.Add('服务端 IP 地址 (IPv4):', False);
  ServerPage.Add('服务端端口:', False);
  ServerPage.Add('服务端共享打印机名称:', False);
  ServerPage.Values[0] := '';
  ServerPage.Values[1] := '17878';
  ServerPage.Values[2] := '';
  // 已注册过（升级/修复安装）：预填上次保存的服务端配置
  Saved := GetSavedServerValue('server_host');
  if Saved <> '' then ServerPage.Values[0] := Saved;
  Saved := GetSavedServerValue('server_port');
  if Saved <> '' then ServerPage.Values[1] := Saved;
  Saved := GetSavedServerValue('share_name');
  if Saved <> '' then ServerPage.Values[2] := Saved;
end;

// 端口字符串校验：1-65535 数字
function IsValidPort(const S: String): Boolean;
var
  PortNum: Integer;
begin
  Result := False;
  if S = '' then Exit;
  try
    PortNum := StrToInt(S);
  except
    Exit;
  end;
  Result := (PortNum >= 1) and (PortNum <= 65535);
end;

// 下一步时校验输入非空 / 端口为数字
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ServerPage.ID then
  begin
    if Trim(ServerPage.Values[0]) = '' then
    begin
      MsgBox('请填写服务端 IP 地址。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if not IsValidPort(Trim(ServerPage.Values[1])) then
    begin
      MsgBox('端口必须为 1-65535 的数字。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if Trim(ServerPage.Values[2]) = '' then
    begin
      MsgBox('请填写服务端共享打印机名称。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
end;

// [Run] register 参数（引用自定义页的输入）
function GetRegisterParams(Param: String): String;
begin
  Result := Format('register --server %s --port %s --share "%s"', [
    Trim(ServerPage.Values[0]), Trim(ServerPage.Values[1]),
    Trim(ServerPage.Values[2])]);
end;

// 安装前连通校验：check-server 退出码 0=可达 / 1=不可达
// 返回非空字符串则中止安装
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResCode: Integer;
  Params: String;
  CheckerPath: String;
begin
  Result := '';
  // 关键时序：本函数在文件复制之前触发，此时安装目录下还没有
  // lanprint_client.exe，必须先解压安装包内的 dontcopy 副本到临时目录
  CheckerPath := ExpandConstant('{tmp}\{#MyAppExeName}');
  ExtractTemporaryFile('{#MyAppExeName}');
  if not FileExists(CheckerPath) then
  begin
    Result := '无法解压连通性检测程序，安装已中止。';
    Exit;
  end;
  Params := 'check-server --host ' + Trim(ServerPage.Values[0]) +
            ' --port ' + Trim(ServerPage.Values[1]);
  // Exec 返回 False 表示检测程序未能启动（区别于"连接失败"）
  if not Exec(CheckerPath, Params, '', SW_HIDE,
              ewWaitUntilTerminated, ResCode) then
  begin
    Result := '连通性检测程序启动失败（' + SysErrorMessage(ResCode) +
      '），安装已中止。';
    Exit;
  end;
  if ResCode <> 0 then
    Result := '无法连接 LANPrint 服务端 ' + Trim(ServerPage.Values[0]) + ':' +
      Trim(ServerPage.Values[1]) + '。' + #13#10 + #13#10 +
      '请检查服务端是否已启动、IP/端口是否正确，以及网络/防火墙设置。' + #13#10 +
      '安装已中止。';
end;

// 生成后台自启动脚本 start_agent_hidden.vbs（{app} 随安装路径替换）
procedure WriteStartVbs;
var
  Content: String;
begin
  Content :=
    ''' LANPrint 客户端后台代理自启动（无窗口运行）' + #13#10 +
    'CreateObject("WScript.Shell").Run """' +
    ExpandConstant('{app}\{#MyAppExeName}') + '"" agent", 0, False' + #13#10;
  SaveStringToFile(ExpandConstant('{app}\start_agent_hidden.vbs'),
                   Content, False);
end;

// 结束正在运行的客户端进程（忽略失败：进程可能不存在）
procedure StopClientProcesses;
var
  ResCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM ' + ExpandConstant('{#MyAppExeName}'),
       '', SW_HIDE, ewWaitUntilTerminated, ResCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResCode: Integer;
  CmdLine: String;
begin
  if CurStep = ssInstall then
  begin
    // 覆盖安装的关键时序：文件复制前必须结束仍在运行的旧 agent，
    // 否则 lanprint_client.exe 被占用，替换时报 DeleteFile failed; code 5（拒绝访问）
    StopClientProcesses;
    Sleep(500);                        // 等待文件句柄释放
  end;
  if CurStep = ssPostInstall then
  begin
    WriteStartVbs;
    // 安装完成即自动执行完整注册（安装器已提权，无需另开管理员窗口）。
    // 交互安装失败时弹窗提示并给出重试命令；静默安装只记录退出码。
    CmdLine := GetRegisterParams('');
    if Exec(ExpandConstant('{app}\{#MyAppExeName}'), CmdLine, '', SW_HIDE,
            ewWaitUntilTerminated, ResCode) then
    begin
      if (ResCode <> 0) and (not WizardSilent) then
        MsgBox('虚拟打印机注册失败（退出码 ' + IntToStr(ResCode) +
          '）。' + #13#10 + #13#10 +
          '可稍后以管理员身份重新运行：' + #13#10 +
          ExpandConstant('{app}\{#MyAppExeName}') + ' ' + CmdLine,
          mbError, MB_OK);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResCode: Integer;
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopClientProcesses;

    // 删除虚拟打印机与端口组件（需管理员，安装器已提权）
    Exec(ExpandConstant('{app}\{#MyAppExeName}'), 'unregister',
         '', SW_HIDE, ewWaitUntilTerminated, ResCode);

    // 可选：删除配置与暂存数据目录
    DataDir := ExpandConstant('{commonappdata}\LANPrint');
    if DirExists(DataDir) then
      if MsgBox('是否同时删除客户端配置与暂存数据？' + #13#10 + DataDir,
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
