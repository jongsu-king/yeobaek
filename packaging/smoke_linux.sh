#!/usr/bin/env bash
set -euo pipefail

if [[ ! ${RELEASE_TAG:-} =~ ^v([0-9]+\.[0-9]+\.[0-9]+)(-rc\.[0-9]+)?$ ]]; then
    printf 'RELEASE_TAG must be a version such as v0.4.0 or v0.4.0-rc.1.\n' >&2
    exit 1
fi
release_version=${BASH_REMATCH[1]}
asset_name="yeobaek_${release_version}_amd64.deb"
download_folder="$RUNNER_TEMP/yeobaek-release"
fixture_folder="$RUNNER_TEMP/yeobaek-installer-data"
script_folder=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
smoke_script="$script_folder/installer_smoke.py"
mkdir -p "$download_folder"
gh release download "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" \
    --pattern "$asset_name" --pattern SHA256SUMS.txt --dir "$download_folder"
(
    cd "$download_folder"
    grep "  ${asset_name}$" SHA256SUMS.txt | sha256sum -c -
)
sudo apt-get update -qq
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    "$download_folder/$asset_name" xvfb xauth xdotool libgl1-mesa-dri fonts-noto-cjk
test "$(dpkg-query -W -f='${Version}' yeobaek)" = "$release_version"
if ldd /opt/yeobaek/client/flet | grep 'not found'; then
    exit 1
fi
bundled_python=/opt/yeobaek/python/bin/python3.12
"$bundled_python" -B -I "$smoke_script" seed --app /opt/yeobaek/app --data "$fixture_folder"
export YEOBAEK_DATA_DIR="$fixture_folder" LIBGL_ALWAYS_SOFTWARE=1
export YEOBAEK_SMOKE_LOG="$RUNNER_TEMP/yeobaek-gui.log"
xvfb-run -a bash -euo pipefail -s -- "$bundled_python" "$smoke_script" "$fixture_folder" <<'UI'
bundled_python=$1
smoke_script=$2
fixture_folder=$3
setsid /usr/bin/yeobaek >"$YEOBAEK_SMOKE_LOG" 2>&1 &
launcher=$!
cleanup() {
    kill -TERM -- "-$launcher" 2>/dev/null || true
    wait "$launcher" 2>/dev/null || true
}
trap cleanup EXIT
window=$(timeout 45s xdotool search --sync --onlyvisible --name '여백' | head -n 1)
sleep 1
xdotool windowfocus --sync "$window"
xdotool key --clearmodifiers ctrl+n
sleep 1
xdotool type --clearmodifiers --delay 30 'Linux installer saved note'
xdotool key --clearmodifiers ctrl+s
sleep 1
"$bundled_python" -B -I "$smoke_script" preserved --data "$fixture_folder" \
    --saved-title 'Linux installer saved note'
printf 'Verified installed Linux GUI created and saved a note through keyboard shortcuts.\n'
UI
sudo apt-get remove -y yeobaek
test ! -e /opt/yeobaek/app/main.py
python3 "$smoke_script" preserved --data "$fixture_folder" --saved-title 'Linux installer saved note'
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$download_folder/$asset_name"
"$bundled_python" -B -I "$smoke_script" check --app /opt/yeobaek/app --data "$fixture_folder"
sudo apt-get remove -y yeobaek
python3 "$smoke_script" preserved --data "$fixture_folder" --saved-title 'Linux installer saved note'
printf 'Verified Linux install, native GUI save, uninstall, reinstall and external note preservation.\n'
