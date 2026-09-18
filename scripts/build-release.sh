#!/bin/bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
config="$repo_root/config/versions.json"
release="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["crossover"]["release"])' "$config")"
archive="Wine-CX-$release-macos-x86_64.tar.xz"

if ! port -N install wine-cx +gstreamer
then
    build_log="$(find /opt/local/var/macports/logs -path '*wine-cx*' -name main.log -print -quit)"
    test -n "$build_log"
    tail -n 500 "$build_log"
    exit 1
fi
for installed_port in \
    wine-cx \
    MoltenVK \
    mingw-w64-wine-mono-10.4.1 \
    mingw-w64-wine-gecko-2.47.4
do
    port -q installed "$installed_port" | grep -q '(active)'
done

python3 "$repo_root/scripts/package-app.py"
python3 "$repo_root/scripts/strip-pe.py"
/usr/bin/xattr -cr "$repo_root/dist/Wine CX.app"
/usr/bin/codesign --force --deep --sign - "$repo_root/dist/Wine CX.app"
python3 "$repo_root/scripts/validate-app.py"

rm -rf "$repo_root/dist/release"
mkdir -p "$repo_root/dist/release"
COPYFILE_DISABLE=1 tar -C "$repo_root/dist" -cf - "Wine CX.app" \
    | xz -T0 -9e > "$repo_root/dist/release/$archive"

license_archive="Wine-CX-$release-LICENSES.tar.xz"
COPYFILE_DISABLE=1 tar -C "$repo_root/dist" -cf - LICENSES \
    | xz -T0 -9e > "$repo_root/dist/release/$license_archive"

source_archive="crossover-sources-$release.tar.gz"
install -m 0644 "$repo_root/build/crossover-sources.tar.gz" \
    "$repo_root/dist/release/$source_archive"

verification="$(mktemp -d "${TMPDIR:-/tmp}/wine-cx-release.XXXXXX")"
trap 'rm -rf "$verification"' EXIT
tar -xf "$repo_root/dist/release/$archive" -C "$verification"
tar -xf "$repo_root/dist/release/$license_archive" -C "$verification"
python3 "$repo_root/scripts/validate-app.py" "$verification/Wine CX.app" "$verification/LICENSES"
du -sh "$repo_root/dist/Wine CX.app" "$repo_root/dist/release/$archive"
