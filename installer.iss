; 狗头军师助手 v2.5 安装程序（开源版）
; 编译：ISCC.exe installer.iss
; 输出：dist\GoutouJunshi_Setup_v2.5.exe

#define MyAppName "狗头军师助手"
#define MyAppVersion "2.5"
#define MyAppExeName "GoutouJunshi.exe"

[Setup]
AppId={{8F5A4B1E-6C2D-4F3A-9B7E-2D1C0A5E6F21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher=狗头军师助手 contributors
DefaultDirName={localappdata}\GoutouJunshi
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=GoutouJunshi_Setup_v{#MyAppVersion}
SetupIconFile=assets\icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
VersionInfoVersion=2.5.0.0
VersionInfoDescription=狗头军师助手安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "dist\GoutouJunshi\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; 更名后清掉旧名字的快捷方式，避免桌面/开始菜单出现两个入口
[InstallDelete]
Type: files; Name: "{autoprograms}\狗头军师 · 回复助手\狗头军师 · 回复助手.lnk"
Type: files; Name: "{autoprograms}\狗头军师 · 回复助手\卸载 狗头军师 · 回复助手.lnk"
Type: files; Name: "{autodesktop}\狗头军师 · 回复助手.lnk"
Type: dirifempty; Name: "{autoprograms}\狗头军师 · 回复助手"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动{#MyAppName}"; Flags: nowait postinstall skipifsilent

; 卸载时不擅自删除 data/（聊天记录、话术库、对象档案都在里面）

