#!/bin/bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
config="$repo_root/config/versions.json"
build="$repo_root/build"
prefix="${1:-/opt/local}"

rm -rf "$build"
mkdir -p "$build"

download_and_check() {
    local url="$1" sha="$2" size="$3" output="$4"
    curl --fail --location --retry 5 --silent --show-error --output "$output" "$url"
    test "$(shasum -a 256 "$output" | awk '{print $1}')" = "$sha"
    test "$(stat -f '%z' "$output")" = "$size"
}

source_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["crossover"]["source_url"])' "$config")"
source_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["crossover"]["source_sha256"])' "$config")"
source_size="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["crossover"]["source_size"])' "$config")"
gstreamer_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gstreamer"]["installer_url"])' "$config")"
gstreamer_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gstreamer"]["installer_sha256"])' "$config")"
gstreamer_size="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gstreamer"]["installer_size"])' "$config")"
devel_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gstreamer"]["development_url"])' "$config")"
devel_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gstreamer"]["development_sha256"])' "$config")"
devel_size="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gstreamer"]["development_size"])' "$config")"

libinotify_portfile="$repo_root/ports/devel/libinotify/Portfile"
mkdir -p "$(dirname "$libinotify_portfile")"
download_and_check \
    "https://raw.githubusercontent.com/Gcenx/macports-wine/63a811f7711646f368f0a7fc18026c8bfe7109bb/devel/libinotify/Portfile" \
    "91374834cdf394c13b2a76846a6e1f1e027c64384f7c31acc1b742825e65203b" \
    1346 "$libinotify_portfile"

download_and_check "$source_url" "$source_sha" "$source_size" "$build/crossover-sources.tar.gz"
download_and_check "$gstreamer_url" "$gstreamer_sha" "$gstreamer_size" "$build/gstreamer-runtime.pkg"
download_and_check "$devel_url" "$devel_sha" "$devel_size" "$build/gstreamer-development.pkg"

sudo /usr/sbin/installer -pkg "$build/gstreamer-runtime.pkg" -target /
sudo /usr/sbin/installer -pkg "$build/gstreamer-development.pkg" -target /
test -x /Library/Frameworks/GStreamer.framework/Commands/pkg-config
test -d /Library/Frameworks/GStreamer.framework/Libraries

sources="$prefix/etc/macports/sources.conf"
temporary="$(mktemp "${TMPDIR:-/tmp}/wine-cx-sources.XXXXXX")"
version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["crossover"]["release"])' "$config")"
portindex "$repo_root/ports"
{
    printf 'file://%s [nosync]\n' "$repo_root/ports"
    cat "$sources"
} > "$temporary"
mv "$temporary" "$sources"

distfiles="$prefix/var/macports/distfiles/wine"
mkdir -p "$distfiles"
install -m 0644 "$build/crossover-sources.tar.gz" "$distfiles/crossover-sources-$version.tar.gz"
port -q info wine-cx
