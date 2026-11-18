# 여백

노트북, 태그, 즐겨찾기, 제목·본문 검색, 휴지통을 갖춘 개인 노트 앱입니다. Python과 SQLite로 노트를 관리하고 Flet으로 데스크톱과 모바일의 네이티브 화면을 표시합니다. 연결이 끊긴 상태에서도 작성하고 검색할 수 있습니다. Markdown 편집·미리보기, 체크리스트, Markdown 내보내기, JSON 백업 및 복원을 지원합니다.

0.2.0에서는 회의록·실험 기록·일지 템플릿, 미완료 할 일과 미분류 보기, 노트북 이름 변경, 노트 복제, 여러 Markdown·텍스트 파일 가져오기, 수정 기록 복원을 지원합니다. 검색창에서 `title:"회의 기록"`, `tag:업무`, `book:프로젝트`, `is:pinned`, `is:task`를 조합할 수 있습니다. 항목 앞에 `-`를 붙이면 그 조건을 제외합니다.

0.3.0에서는 노트 상단의 클립 아이콘으로 모든 확장자의 파일을 첨부할 수 있습니다. 한 파일은 64 MiB까지 지원합니다. 첨부 이름을 누르면 PDF 페이지, Excel 시트의 셀, CSV·TSV 표, 일반 이미지와 텍스트가 노트 안에 표시됩니다. 첨부 파일 메뉴에서 원본을 저장하거나 다른 앱으로 열 수 있습니다. 첨부 파일은 Drive와 전체 백업에도 포함되며 `is:file`로 찾을 수 있습니다.

∑ 버튼으로 LaTeX를 삽입하거나, 본문에 `$E=mc^2$`처럼 작성하세요. 여러 줄 수식은 시작과 끝에 `$$`를 각각 별도 줄에 적습니다. **미리보기**를 누르면 수식이 렌더링됩니다.

0.3.1에서는 데스크톱 창을 닫을 때 대기 중인 편집 내용을 먼저 저장합니다. 저장에 실패하면 창과 입력 내용을 유지해 다시 저장할 수 있습니다. 첨부 전송 중 Google 인증이 만료되면 기존 모바일 로그인 갱신 처리를 거쳐 다시 동기화합니다. 행·열 범위를 잘못 기록한 Excel 파일과 CP949 한글 CSV·TSV, 긴 셀의 미리보기도 개선했습니다.

0.4.0에서는 **노트 메뉴 → 첨부와 함께 ZIP 내보내기**로 Markdown과 첨부 원본을 함께 보관할 수 있습니다. 압축을 풀면 `note.md`가 `attachments/`의 파일을 상대 경로로 참조합니다. 파일이 누락되거나 손상되면 내보내기 전에 알립니다. PDF·이미지는 확대·축소·이동과 확대 초기화를 지원합니다.

**노트 메뉴 → 본문에서 찾기** 또는 `Ctrl/Cmd+F`로 현재 본문을 검색합니다. 검색창에서 Enter를 누르면 일치하는 본문을 선택합니다. 다음·이전 버튼, F3·Shift+F3 또는 `Ctrl/Cmd+G`·`Ctrl/Cmd+Shift+G`로 이동하고 Escape로 닫습니다. 백업·파일 저장은 임시 파일을 완성한 뒤 교체하며, 첨부 원본을 읽을 때 무결성을 확인합니다. 손상된 로컬 첨부는 보존하고, 다음 동기화에서 정상 원본이 있으면 받아 복구합니다.

## 설치 파일

[GitHub 최신 릴리스](https://github.com/jongsu-king/yeobaek/releases/latest)에서 운영체제에 맞는 파일을 받으세요. 함께 제공하는 `SHA256SUMS.txt`로 다운로드한 파일의 SHA-256을 확인할 수 있습니다.

| 플랫폼 | 파일 | 확인한 범위 |
|---|---|---|
| macOS Apple Silicon | `Yeobaek-0.4.0-macOS-arm64.zip` | 압축을 풀어 `Yeobaek.app` 실행. Apple 공증 미실행 |
| Android ARM64 | `Yeobaek-0.4.0-Android-arm64.apk` | Android 7.0 이상. 테스트 인증서 서명·무결성 확인, 실기기 실행 미검증 |
| Windows x64 | `Yeobaek-0.4.0-Windows-x64-Setup.exe` | 설치 파일 구조 확인, Windows 실행 미검증. 코드 서명 없음 |
| Debian·Ubuntu ARM64 | `yeobaek_0.4.0_arm64.deb` | 0.3.0을 Ubuntu 24.04 가상 머신에서 설치·실행·저장 확인 |
| Debian·Ubuntu AMD64 | `yeobaek_0.4.0_amd64.deb` | 패키지 구조 확인, 해당 아키텍처에서 실행 미검증 |
| iOS 시뮬레이터 ARM64 | `Yeobaek-0.4.0-iOS-Simulator-arm64.zip` | 0.3.0을 iOS 26.4 시뮬레이터에서 설치·PDF 페이지 이동 확인 |

iPhone 실기기용 설치 파일은 Apple 서명 인증서와 프로비저닝 프로파일이 있어야 만들 수 있습니다. Android와 iPhone 실기기 간 Drive 동기화는 미검증입니다. Mac과 Ubuntu 가상 머신 사이에서는 실제 Google Drive로 PDF·Excel을 양방향 전송해 원본 바이트, PDF 이미지와 Excel 셀을 확인했습니다.

0.3.0의 Android 네이티브 빌드는 APK 생성 뒤 Gradle 캐시 저장 오류가 발생했습니다. 이후 Python 수정판은 검증된 네이티브 런타임을 재사용하며 배포 APK의 서명과 포함 파일을 검사합니다.

## 실행

Mac에서는 빌드한 `dist/Yeobaek.app`을 엽니다. 소스에서 실행하려면 Python 3.12와 [uv](https://docs.astral.sh/uv/)를 준비하고 이 폴더에서 실행합니다.

```sh
uv sync --python 3.12 --extra desktop-preview
uv run python main.py
```

앱 데이터는 운영체제의 애플리케이션 지원 디렉터리 아래 `Yeobaek/notes.sqlite3`에 저장됩니다. `YEOBAEK_DATA_DIR`를 설정하면 별도 경로에서 실험할 수 있습니다.

직접 빌드한 설치 파일은 `dist`에 생성됩니다. [Windows·Linux 설치 및 빌드 안내](packaging/README.md)

## Google Drive 설정

모든 기기에서 같은 Google 계정과 같은 Google Cloud 프로젝트의 OAuth 클라이언트를 사용합니다. 노트 수정 이력은 Drive의 [앱 데이터 폴더](https://developers.google.com/workspace/drive/api/guides/appdata)에 저장됩니다. 동시 수정이 발견되면 **노트 메뉴 → 충돌한 버전 보기**에서 적용할 버전을 선택하거나 별도 노트로 복사할 수 있습니다.

1. [Google Cloud 콘솔](https://console.cloud.google.com/)에서 프로젝트를 만들고 Google Drive API를 사용 설정합니다.
2. Google 인증 플랫폼에서 앱 이름과 대상, 테스트 사용자를 설정합니다. 데이터 액세스 범위는 `https://www.googleapis.com/auth/drive.appdata`를 사용합니다.
3. Mac, Windows, Linux에서는 **데스크톱 앱** 유형의 OAuth 클라이언트를 만들고 JSON을 다운로드합니다.
4. 여백의 **Google Drive → Drive 연결**에서 JSON을 선택한 뒤 시스템 브라우저에서 권한을 허용합니다. `YEOBAEK_GOOGLE_CLIENT_JSON`에 설정 파일 경로를 지정할 수도 있습니다.
5. Android와 iOS는 각 플랫폼용 OAuth 클라이언트와 빌드 구성이 필요합니다. [모바일 설정](mobile_auth/README.md)을 참고하세요.

제공하는 모바일 빌드는 개발용 Google OAuth 구성으로 만들었습니다. Google 프로젝트가 테스트 상태인 동안에는 등록된 테스트 사용자만 로그인할 수 있습니다. 자신의 Google 계정과 프로젝트로 모바일을 사용하려면 위 설정을 적용해 다시 빌드하세요.

연결된 앱은 저장 후 동기화를 요청하고 30초마다 Drive의 변경을 확인합니다. 일시적인 네트워크 오류에는 재시도하며, 서버에서 일부 기록을 읽을 수 없으면 동기화 상태에 개수를 표시합니다. 데스크톱 앱은 마지막으로 연결한 OAuth JSON의 위치를 기억합니다. 토큰은 실행 중 메모리에 보관하므로 재실행 후 **Drive 연결**을 누릅니다.

## 패키징

[Flet 빌드 도구](https://flet.dev/docs/publish/)로 대상 운영체제의 빌드를 만들 수 있습니다. Windows와 Debian 계열 Linux 설치 파일은 사전 빌드된 대상 런타임을 묶는 [패키징 도구](packaging/README.md)로도 만듭니다.

| 타깃 | 프로젝트 폴더에서 실행 |
|---|---|
| macOS | `uv run flet build macos` |
| Windows | `uv run flet build windows` |
| Linux | `uv run flet build linux` |
| Android | `uv run flet build apk` |
| iOS 시뮬레이터 | `uv run flet build ios-simulator` |
| iPhone·iPad | Apple 개발자 팀과 서명 설정 후 `uv run flet build ipa` |

Mac의 PyInstaller 패키징 방법은 `uv pip install pyinstaller` 후 `uv run flet pack main.py --name Yeobaek --icon assets/icon.icns --bundle-id io.github.jongsuking.yeobaek --product-version 0.4.0 --yes -- --collect-all pypdfium2_raw`입니다. Python 코드 수정판은 [기존 네이티브 런타임을 재사용하는 패키징 도구](packaging/README.md#python-코드-수정판-만들기)로 만들 수도 있습니다.

## 구현 참고

[REA 6.8.0](https://github.com/morluto/rea)의 브라우저 조사 도구로 [Evernote 한국어 공개 사이트](https://evernote.com/ko-kr)를 읽었습니다. 공개 기능 안내의 노트북, 태그, 검색, 오프라인 편집과 동기화 흐름을 바탕으로 여백을 구현했습니다.
