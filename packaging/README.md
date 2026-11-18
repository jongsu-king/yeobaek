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
sudo apt install ./yeobaek_0.4.0_amd64.deb
```

구글 드라이브에 연결할 때는 데스크톱 OAuth JSON을 선택하세요. 계정 설정과 메모는 앱을 실행할 기기에서 관리합니다. [Google Drive 설정 안내](../README.md#google-drive-설정)

[Flet 릴리스](https://github.com/flet-dev/flet/releases/tag/v1.1.2) · [Python 런타임 릴리스](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110) · [NSIS](https://nsis.sourceforge.io/)

## macOS Apple Silicon

기존 Flet·PyInstaller 환경에서 다음 명령을 실행하면 `pyproject.toml` 버전의 ARM64 ZIP을 만듭니다. `--build-number`를 생략하면 번들 빌드 번호에도 프로젝트 버전을 사용합니다.

```sh
uv pip install pyinstaller
uv run python packaging/build_macos.py --build-number 3
```

메인 소스와 아이콘의 절대 경로를 사용해 임시 폴더에서 빌드하고, 바깥 앱 번들의 버전 정보를 보정한 뒤 임시 서명과 엄격한 서명 검사를 수행합니다. PDFium 수집 옵션은 유지합니다. 완성한 ZIP은 출력 폴더에 원자적으로 게시하며 기존 `dist/Yeobaek.app`을 교체하지 않습니다. 작업 위치는 `--work-dir`, 출력 위치는 `--output`으로 지정합니다.

## Python 코드 수정판 만들기

Python 코드만 수정한 릴리스는 `refresh_release.py`로 검증된 이전 설치 파일의 네이티브 런타임을 재사용합니다. Git 태그가 있는 소스 체크아웃과 Python 3.12가 필요합니다. `pyproject.toml`의 버전을 먼저 올리고, 이전 릴리스 파일과 체크섬을 별도 폴더에 준비하세요. 스크립트는 기준 태그와 의존성·Flutter 확장을 비교하고, 변경된 경우 전체 빌드를 요구합니다.

```sh
python3.12 packaging/refresh_release.py linux-amd64 linux-arm64 \
  --base-version 0.3.0 --base-dir /path/to/0.3.0 --output dist
```

기준 체크섬은 `SHA256SUMS-0.3.0.txt`, `SHA256SUMS.txt` 순서로 찾습니다. `--baseline-checksums /path/to/checksums.txt`로 직접 지정할 수도 있습니다. 새 파일의 SHA-256과 사용한 원본·소스의 SHA-256은 실행 결과에 출력됩니다.

새로 추가한 `yeobaek` Python 모듈도 각 설치 파일에 포함합니다. 완성된 파일을 출력 폴더 안의 임시 파일로 복사한 뒤 원자적으로 교체하므로, 작업 디스크 연결이 끊기거나 복사에 실패하면 기존 배포 파일을 보존합니다.

- `ios-simulator` 대상은 macOS의 `ditto`·`codesign`을 사용해 Python 코드와 버전을 갱신하고 임시 서명을 다시 적용합니다.
- `windows` 대상에는 `--sevenzip /path/to/7zz --makensis /path/to/makensis`를 지정합니다. NSIS를 별도 폴더에 풀어 사용하면 그 폴더의 `share/nsis`를 `NSISDIR` 환경 변수로 설정하세요.
- `android` 대상에는 공식 Android SDK build-tools 폴더와 기존 앱의 서명 키가 필요합니다. JDK를 `JAVA_HOME`에, 키스토어·키 비밀번호를 `YEOBAEK_ANDROID_STORE_PASSWORD` 환경 변수에 설정하세요. 아래 명령의 versionCode는 이전 APK보다 큰 정수여야 합니다. 새 APK의 인증서가 기존 APK와 같은지, 16 KiB 정렬과 패키지 버전을 확인한 뒤 결과물을 저장합니다.

```sh
python3.12 packaging/refresh_release.py android \
  --base-version 0.3.0 --base-dir /path/to/0.3.0 --output dist \
  --android-tools /path/to/android-sdk/build-tools/36.0.0 \
  --android-version-code 2 --keystore /path/to/keystore \
  --key-alias androiddebugkey
```

키스토어와 비밀번호는 저장소에 커밋하지 마세요. 배포 파일을 확인한 뒤 새 릴리스용 `SHA256SUMS.txt`를 생성합니다.

## 설치 파일 검사

GitHub의 `Installer smoke tests` 워크플로는 릴리스 게시 시 Windows x64와 Ubuntu AMD64에서 설치·앱 실행·제거·재설치를 검사합니다. 설치 폴더 밖에 만든 합성 노트와 첨부 파일이 보존되는지 확인하고, 설치된 Python으로 PDF 이미지와 Excel 셀도 확인합니다. 0.4.0부터 설치된 ZIP 내보내기 모듈의 상대 링크·원본 바이트·원자적 저장도 검사합니다. Linux는 키보드로 새 노트를 작성하고 저장합니다. 직접 실행할 때는 검사할 태그를 지정하세요.

```sh
gh workflow run installers.yml -f release_tag=v0.4.0
```

`v0.4.0-rc.1`처럼 사전 릴리스 태그도 지정할 수 있습니다. 이때 다운로드 태그는 그대로 사용하고 설치 파일 이름과 설치 버전은 `0.4.0`으로 검사합니다.
