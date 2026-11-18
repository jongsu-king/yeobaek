Unicode true
!include "x64.nsh"
!include "LogicLib.nsh"

!define INSTALL_ROOT "$LOCALAPPDATA\Programs\Yeobaek"
!define INSTALL_PATH "${INSTALL_ROOT}\${APP_VERSION}"
!define PRODUCT_LABEL "Yeobaek ${APP_VERSION}"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Yeobaek-${APP_VERSION}"

Name "${PRODUCT_LABEL}"
OutFile "${OUT_FILE}"
InstallDir "${INSTALL_PATH}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "${ICON_FILE}"
UninstallIcon "${ICON_FILE}"
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "Yeobaek"
VIAddVersionKey "CompanyName" "Yeobaek"
VIAddVersionKey "LegalCopyright" "Yeobaek"
VIAddVersionKey "FileDescription" "Yeobaek installer"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"

Var StageDir

!macro StopWithMessage TEXT
  MessageBox MB_OK|MB_ICONSTOP "${TEXT}" /SD IDOK
  SetErrorLevel 1
  Abort
!macroend

!macro RejectReparse PATH
  System::Call 'kernel32::GetFileAttributesW(w "${PATH}") i.r0'
  ${If} $0 != -1
    IntOp $0 $0 & 0x400
    ${If} $0 != 0
      !insertmacro StopWithMessage "Yeobaek cannot use an install folder that is a link or junction."
    ${EndIf}
  ${EndIf}
!macroend

!macro VerifyInstallPath
  SetShellVarContext current
  ${If} $INSTDIR != "${INSTALL_PATH}"
    !insertmacro StopWithMessage "Run the installer with its default folder, or start the installed uninstaller from Windows Settings."
  ${EndIf}
  !insertmacro RejectReparse "$LOCALAPPDATA\Programs"
  !insertmacro RejectReparse "${INSTALL_ROOT}"
  !insertmacro RejectReparse "$INSTDIR"
!macroend

!macro AcquireGuard
  System::Call 'kernel32::SetLastError(i 0)'
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "${MUTEX_NAME}") p.r2 ?e'
  Pop $3
  ${If} $2 == 0
    !insertmacro StopWithMessage "Yeobaek could not start setup. Close the app and any setup windows, then try again."
  ${ElseIf} $3 == 183
    System::Call 'kernel32::CloseHandle(p r2)'
    !insertmacro StopWithMessage "Close Yeobaek and any Yeobaek setup windows, then try again."
  ${EndIf}
!macroend

!include "${OWNED_MANIFEST}"

Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

Function .onInit
  ${IfNot} ${RunningX64}
    !insertmacro StopWithMessage "Yeobaek needs a 64-bit Windows system."
  ${EndIf}
  !insertmacro VerifyInstallPath
  !insertmacro AcquireGuard
  System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR") i.r0'
  ${If} $0 != -1
    !insertmacro StopWithMessage "This Yeobaek version already has an install folder. Uninstall it from Windows Settings before reinstalling."
  ${EndIf}
FunctionEnd

Function CleanStage
  ${If} $StageDir != ""
    SetOutPath "$TEMP"
    !insertmacro DeleteOwnedFiles "$StageDir"
    Delete "$StageDir\Uninstall.exe"
    !insertmacro RemoveEmptyDirectories "$StageDir"
    Delete "$StageDir"
    RMDir "$StageDir"
    StrCpy $StageDir ""
  ${EndIf}
FunctionEnd

Section "Yeobaek"
  ClearErrors
  CreateDirectory "${INSTALL_ROOT}"
  GetTempFileName $StageDir "${INSTALL_ROOT}"
  IfErrors stage_failed
  Delete "$StageDir"
  CreateDirectory "$StageDir"
  IfErrors stage_failed

  SetOutPath "$StageDir\python"
  File /r "${PAYLOAD}/python/*"
  IfErrors stage_failed
  SetOutPath "$StageDir\client"
  File /r "${PAYLOAD}/client/*"
  IfErrors stage_failed
  SetOutPath "$StageDir\app"
  File /r "${PAYLOAD}/app/*"
  IfErrors stage_failed
  WriteUninstaller "$StageDir\Uninstall.exe"
  IfErrors stage_failed

  SetOutPath "$TEMP"
  Rename "$StageDir" "$INSTDIR"
  IfErrors stage_failed
  StrCpy $StageDir ""

  SetOutPath "$INSTDIR\app"
  CreateShortCut "$SMPROGRAMS\${PRODUCT_LABEL}.lnk" "$INSTDIR\python\pythonw.exe" '-B -I "$INSTDIR\app\launch.py"' "$INSTDIR\app\yeobaek.ico"
  IfErrors registration_failed
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT_LABEL}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\app\yeobaek.ico"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  IfErrors registration_failed
  Goto install_finished

  registration_failed:
    Delete "$SMPROGRAMS\${PRODUCT_LABEL}.lnk"
    DeleteRegKey HKCU "${UNINSTALL_KEY}"
    StrCpy $StageDir $INSTDIR
  stage_failed:
    Call CleanStage
    !insertmacro StopWithMessage "Yeobaek setup did not finish. Check free space and folder access, then try again."
  install_finished:
SectionEnd

Function .onInstFailed
  Call CleanStage
FunctionEnd

Function un.onInit
  !insertmacro VerifyInstallPath
  !insertmacro AcquireGuard
  !insertmacro VerifyOwnedDirectories "$INSTDIR"
FunctionEnd

Section "Uninstall"
  SetOutPath "$TEMP"
  ClearErrors
  !insertmacro DeleteOwnedFiles "$INSTDIR"
  ${If} ${Errors}
    !insertmacro StopWithMessage "Some Yeobaek files are in use. Close programs that have the install folder open, then run Uninstall again."
  ${EndIf}
  !insertmacro RemoveEmptyDirectories "$INSTDIR"
  Delete "$SMPROGRAMS\${PRODUCT_LABEL}.lnk"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  RMDir "${INSTALL_ROOT}"
SectionEnd
