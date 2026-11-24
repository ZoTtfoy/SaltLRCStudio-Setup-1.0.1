; SaltLRC Studio —— 安装向导（Inno Setup 6）
;
; 编译（两种都行）：
;   ISCC.exe packaging\installer.iss /DAppVersion=x.y.z
;   powershell -ExecutionPolicy Bypass -File packaging\release.ps1        ← 一条命令做完整个发布
;
; 产物：dist\SaltLRCStudio-Setup-<版本>.exe
;
; 设计上的几条硬规矩（和程序本身一致）：
;   * **按用户安装**（PrivilegesRequired=lowest）：不需要管理员、不写 Program Files，
;     装在 %LOCALAPPDATA%\Programs\SaltLRC Studio —— 和"本机工具、不上传任何东西"的定位一致；
;   * **卸载不碰用户数据**：凭据、设置、本地工程都在 %LOCALAPPDATA%\SaltLRCStudio\，
;     卸载器**不删**它们（见文件末尾的说明）。卸载后又重装，登录态与设置都还在。
;   * 安装包里带**使用说明与开发者文档**（用户报 bug 时能直接对照版本）。

#define AppName "SaltLRC Studio"
#define AppExe "SaltLRCStudio.exe"
#define AppPublisher "SaltLRC Studio"
; 版本号由 release.ps1 用 /DAppVersion= 传入；单独编译时用这里的默认值。
; ⚠️ 唯一权威仍是 saltlrc\__init__.py —— tests/test_packaging.py 会比对这个默认值。
#ifndef AppVersion
  #define AppVersion "1.0.5"
#endif
; 稳定不变的应用 ID：升级安装靠它认出"这是同一个程序"（别改！）
; Inno 的 `{{` 是转义成一个字面左大括号，所以这里**只有一个**收尾的 `}`：
; 它的值是 {7C1F2D34-9B5A-4E71-A6C3-2F0B9D8E4A15}。写成 `{{GUID}}` 会多出一个字面 `}`。
#define AppId "{{7C1F2D34-9B5A-4E71-A6C3-2F0B9D8E4A15}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DefaultGroupName={#AppName}
; 按用户安装：不需要管理员权限
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=SaltLRCStudio-Setup-{#AppVersion}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 装完就能听到声音：程序自己会开窗口，所以不要求重启
RestartIfNeededByRun=no

; 向导语言：默认只带英文（Inno 自带）。
; 装了简体中文语言包的话，把下面这行换成：
;   Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
; 主程序（单文件，已带图标）
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
; 工程文件（.saltproj）的图标。**必须和 exe 同目录**：程序运行时会把关联里的
; DefaultIcon 指向这个文件（见 saltlrc/assoc.py）。少了它，图标会静默退回程序
; 自身的图标 —— 所以这里不能省。
Source: "icons\SaltLRCStudio-project.ico"; DestDir: "{app}"; Flags: ignoreversion
; 说明与文档。用户报 bug 时能直接对照自己这一版改了什么。
Source: "..\dist\使用说明.txt";          DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\RELEASE_NOTES.md";      DestDir: "{app}"; DestName: "修改记录.md";     Flags: ignoreversion
Source: "..\docs\CHANGELOG.md";          DestDir: "{app}"; DestName: "更新日志.md";     Flags: ignoreversion
Source: "..\docs\ARCHITECTURE.md";       DestDir: "{app}"; DestName: "分层结构.md";     Flags: ignoreversion
Source: "..\docs\WINDOW_STRUCTURE.md";   DestDir: "{app}"; DestName: "窗口结构.md";     Flags: ignoreversion
Source: "..\docs\AUDIO_ALIGNMENT.md";    DestDir: "{app}"; DestName: "音频对齐说明.md"; Flags: ignoreversion
Source: "..\README.md";                  DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md";     DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\使用说明";   Filename: "{app}\使用说明.txt"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; 装完可以直接开。勾选框默认勾上 —— 装完就想用。
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 程序是"关掉窗口即退出"，卸载前不用额外收尾。

; ⚠️ 这里**故意没有** [UninstallDelete]：
;   凭据与设置（%LOCALAPPDATA%\SaltLRCStudio\credentials.json / settings.json）
;   以及本地工程库的保存路径与工程文件，**卸载时不删**。
;   理由：卸载一个工具不该顺手删掉用户的登录态和已经落盘的工程 ——
;   那是不可逆的，而且用户没有任何办法预见。
;   （想彻底清干净：自己删 %LOCALAPPDATA%\SaltLRCStudio 和工程保存目录，使用说明里写了。）
