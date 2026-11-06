; 文件索引工具 —— Inno Setup 安装包脚本（Windows Release 资产）
; onedir 目录仅作为构建中间产物，不在 Release 中直接发布。
;
; 本地手工验证（已在 Windows 上实测通过）：
;   1) pyinstaller build.spec --noconfirm           → dist\文件索引工具\
;   2) 安装 Inno Setup 6：https://jrsoftware.org/isdl.php
;      或 winget install JRSoftware.InnoSetup（装到用户目录，无需管理员）
;   3) "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" /DMyAppVersion=1.2.3 installer\file-xray.iss
;      （全局安装时路径为 C:\Program Files (x86)\Inno Setup 6\ISCC.exe）
;   4) 产物：dist\installer\file-xray-<版本>-windows-x64-setup.exe
;
; CI 由 tag 推导版本后通过 /DMyAppVersion 注入；未注入时用下面的默认值。
;
; 数据与日志落在 %USERPROFILE%\file_index_data\（DB、logs\）与
; %USERPROFILE%\.file_xray_token，卸载时保留，避免误删索引。

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "文件索引工具"
#define MyAppPublisher "File X-Ray"
#define MyAppExeName "文件索引工具.exe"
#define DistDir "..\dist\文件索引工具"

; VersionInfoVersion 只接受数字段，tag 若带预发布后缀（如 1.2.3-rc1）先截掉再写入，
; AppVersion 仍保留完整版本号用于显示。
#if Pos("-", MyAppVersion) > 0
  #define MyAppVersionNumeric Copy(MyAppVersion, 1, Pos("-", MyAppVersion) - 1)
#else
  #define MyAppVersionNumeric MyAppVersion
#endif

[Setup]
AppId={{8F1C0D52-6A47-4C3B-9B5E-2E0F0B1A9C11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersionNumeric}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\FileXRay
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 按用户安装：不弹 UAC
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=file-xray-{#MyAppVersion}-windows-x64-setup
SetupIconFile=..\assets\app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
; Inno Setup 6.7.x 发行包不再内置简体中文语言文件（实测 6.7.3 的 Languages 目录里没有
; ChineseSimplified.isl），故把官方源文件随仓库放在 installer\ChineseSimplified.isl，
; 本地与 CI 都用同一份，避免“本机能编、CI 编不过”的差异。
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[InstallDelete]
; 升级时先清掉旧的 onedir 运行库，避免残留旧 DLL 与新版本混用
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs


[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 仅清理安装目录内的运行产物；用户目录下的数据库/日志保留
Type: filesandordirs; Name: "{app}\file_index_data"
