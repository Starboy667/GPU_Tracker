#!/usr/bin/env sh
# Rebuilds pricewatch/web/static/vendor/three-<version>.min.js: three.js and the add-ons listed in
# tools/vendor-three-entry.js, as one minified ES module, and records its fingerprint in
# tools/vendor-three.sha256 (a test compares the two).
#
# The pages only run scripts served by the app itself (Content-Security-Policy: script-src 'self'),
# so the library is committed to the repository instead of being fetched from a CDN at run time.
# Needs Node.js and access to the npm registry. Nothing is installed in the repository, and no
# package gets to run an install script.
#
# To move to another release: change the two THREE_ lines below (npm view three@X dist.integrity),
# run this script, and update the file name in pricewatch/web/static/landing/three.js.
set -eu

THREE_VERSION="0.187.0"
THREE_INTEGRITY="sha512-Qiu7R+Cmh8CstZsY1uJOSWnVlfo7ZtNLV7mF8Jltk3LB3AtDu9cRZlcnRKkAk8Y4z6KKw35z7Y7Urd7jWY8etA=="
ESBUILD_VERSION="0.28.2"
ESBUILD_INTEGRITY="sha512-HKVLS8dvII+xoKW9kmqxbRKrnWEXfJJr/FZhhJmiqIB0e053QNYFqOBouTMO/k5sID4MvCiUCvv8b9M4h32wIA=="

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="three-$THREE_VERSION.min.js"
OUT="$ROOT/pricewatch/web/static/vendor/$NAME"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The registry must still be handing out the archives these fingerprints were taken from.
check() {
  found="$(npm view "$1" dist.integrity)"
  if [ "$found" != "$2" ]; then
    echo "$1: the registry now serves a different archive ($found). Stopping." >&2
    exit 1
  fi
}
check "three@$THREE_VERSION" "$THREE_INTEGRITY"
check "esbuild@$ESBUILD_VERSION" "$ESBUILD_INTEGRITY"

cd "$WORK"
npm init -y >/dev/null
npm install --silent --no-audit --no-fund --ignore-scripts "three@$THREE_VERSION" "esbuild@$ESBUILD_VERSION"
cp "$ROOT/tools/vendor-three-entry.js" entry.js
mkdir -p "$(dirname "$OUT")"
./node_modules/.bin/esbuild entry.js --bundle --format=esm --minify --target=es2020 \
  --legal-comments=inline --log-level=warning --outfile="$OUT"
(cd "$(dirname "$OUT")" && sha256sum "$NAME") > "$ROOT/tools/vendor-three.sha256"
printf 'built %s (%s bytes, %s gzipped)\n' "$NAME" "$(wc -c < "$OUT")" "$(gzip -9 -c "$OUT" | wc -c)"
