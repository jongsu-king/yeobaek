# Windows·Linux 설치 파일 빌드

Windows x64용 NSIS 설치 프로그램과 Debian 계열 Linux용 `.deb` 패키지를 만듭니다. 공식 Flet 데스크톱 실행 파일, 대상 운영체제용 Python 3.12, 앱 의존성을 묶으므로 실행할 컴퓨터에는 Python을 따로 설치할 필요가 없습니다.

macOS 또는 Linux 빌드 컴퓨터에 Python 3.12, `uv`, `ar`을 준비하세요. Windows 설치 프로그램을 만들려면 `makensis`도 준비합니다. 여백 프로젝트 폴더에서 실행하면 공식 배포 파일의 SHA-256을 확인하고 `dist`에 설치 파일을 저장합니다.

```sh
python3.12 packaging/build_desktop.py windows-x64 linux-amd64 linux-arm64 \
  --cache /tmp/yeobaek-desktop-cache
```

Windows 10·11 x64에서는 `Yeobaek-…-Windows-x64-Setup.exe`를 실행한 뒤 시작 메뉴에서 해당 버전의 **Yeobaek**을 엽니다. 앱을 닫은 뒤 새 버전을 설치하거나 Windows 설정에서 기존 버전을 제거하세요. 설치 프로그램에는 코드 서명이 없습니다.

Ubuntu 22.04와 Debian 12 이상에서는 컴퓨터의 아키텍처에 맞는 패키지를 고르세요. `apt`가 GUI 실행에 필요한 시스템 패키지를 설치합니다. 설치 후 앱 메뉴에서 **Yeobaek**을 엽니다.

```sh
sudo apt install ./yeobaek_0.3.0_amd64.deb
```

구글 드라이브에 연결할 때는 데스크톱 OAuth JSON을 선택하세요. 계정 설정과 메모는 앱을 실행할 기기에서 관리합니다. [Google Drive 설정 안내](../README.md#google-drive-설정)

[Flet 릴리스](https://github.com/flet-dev/flet/releases/tag/v1.1.2) · [Python 런타임 릴리스](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110) · [NSIS](https://nsis.sourceforge.io/)
