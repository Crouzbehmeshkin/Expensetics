#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #error SourceDir must point to the PyInstaller one-folder payload
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
AppId={{D506E9B0-508C-45B6-8E95-75EF41A36946}
AppName=Expensetics
AppVersion={#AppVersion}
AppPublisher=Expensetics
DefaultDirName={localappdata}\Programs\Expensetics
DefaultGroupName=Expensetics
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Expensetics-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Expensetics.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Expensetics"; Filename: "{app}\Expensetics.exe"
Name: "{autodesktop}\Expensetics"; Filename: "{app}\Expensetics.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Expensetics.exe"; Description: "Launch Expensetics"; Flags: nowait postinstall skipifsilent
