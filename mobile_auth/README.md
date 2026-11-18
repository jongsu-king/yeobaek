# 모바일 Google Drive 로그인

이 Flet 확장은 Android와 iOS의 Google 로그인 SDK에서 `drive.appdata` 액세스 토큰을 받아 여백의 Python 동기화 코드로 전달합니다. 같은 Google 계정과 같은 Google Cloud 프로젝트의 앱이 Drive의 전용 앱 데이터에 접근합니다. [Drive 앱 데이터](https://developers.google.com/workspace/drive/api/guides/appdata)

## Google Cloud 설정

하나의 Google Cloud 프로젝트에서 Google Drive API를 활성화하고 OAuth 동의 화면에 `https://www.googleapis.com/auth/drive.appdata` 범위를 추가하세요. 외부 앱을 테스트 상태로 두는 경우 실제 로그인할 Google 계정을 테스트 사용자로 등록하세요.

- Android용 OAuth 클라이언트를 만들고 설치할 APK 또는 AAB의 앱 패키지 이름과 **실제 배포 서명 인증서의 SHA-1**을 등록하세요. 같은 프로젝트의 웹 애플리케이션 OAuth 클라이언트 ID를 `yeobaek-python/assets/mobile_oauth.json`의 `server_client_id`에 넣으세요. [Android 플러그인 설정](https://pub.dev/packages/google_sign_in_android)
- iOS용 OAuth 클라이언트를 만들고 앱의 번들 ID를 등록하세요. `yeobaek-python/assets/mobile_oauth.json`의 `ios_client_id`와 `yeobaek-python/pyproject.toml`의 `[tool.flet.ios.info]`에 같은 클라이언트 ID를 넣으세요. URL scheme에는 뒤집은 클라이언트 ID를 넣은 뒤 다시 빌드하세요. [iOS 플러그인 설정](https://pub.dev/packages/google_sign_in_ios)

```toml
[tool.flet.ios.info]
GIDClientID = "YOUR_IOS_CLIENT_ID.apps.googleusercontent.com"
CFBundleURLTypes = [
    { CFBundleTypeRole = "Editor", CFBundleURLSchemes = ["com.googleusercontent.apps.YOUR_IOS_CLIENT_ID"] },
]
```

```json
{
    "server_client_id": "YOUR_WEB_CLIENT_ID.apps.googleusercontent.com",
    "ios_client_id": "YOUR_IOS_CLIENT_ID.apps.googleusercontent.com"
}
```

`mobile_oauth.json`에는 공개 클라이언트 ID만 넣으세요.

## 빌드

`yeobaek-python`에서 아래 명령을 실행하세요. Android는 JDK 17과 Android SDK가 필요합니다. iOS는 macOS와 Xcode가 필요합니다. 시뮬레이터 앱은 서명 없이 빌드하고, 실제 기기용 IPA에는 Apple 서명과 프로비저닝이 필요합니다. [Android 빌드](https://flet.dev/docs/publish/android/) · [iOS 빌드](https://flet.dev/docs/publish/ios/)

```sh
flet build apk
flet build ios-simulator
flet build ipa
```

Flet은 모바일 빌드에서 `mobile_auth`를 설치하고 Python 패키지에 포함된 Flutter 확장을 등록합니다. Google 로그인을 테스트할 때는 위 명령으로 빌드한 앱을 사용하세요. `flet run`의 일반 클라이언트에는 이 확장이 들어 있지 않습니다.
