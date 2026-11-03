#!/usr/bin/env bash
# Make a release: bump the version, run the tests, commit, tag and push.
# GitHub Actions (.github/workflows/release.yml) then builds and publishes it, and installed
# copies of Ditto offer the update within a day.
#
#   scripts/release.sh 1.3.0 "Short description of what changed"
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VER="${1:?usage: scripts/release.sh X.Y.Z \"release notes\"}"
NOTES="${2:-}"
[[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must look like 1.2.3" >&2; exit 1; }
[[ "$(git rev-parse --abbrev-ref HEAD)" == main ]] || { echo "switch to main first" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "commit or stash your changes first" >&2; exit 1; }
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || { echo "main is not in sync with origin" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/v$VER" >/dev/null && { echo "tag v$VER already exists" >&2; exit 1; }
OLD=$(python3 -c "import ditto_linux; print(ditto_linux.__version__)")

sed -i "s/__version__ = \"$OLD\"/__version__ = \"$VER\"/" ditto_linux/__init__.py
sed -i "s/^version = \"$OLD\"/version = \"$VER\"/" pyproject.toml
sed -i "s/ditto-linux_${OLD}_all\.deb/ditto-linux_${VER}_all.deb/g; s/ditto-linux-${OLD}\.tar\.gz/ditto-linux-${VER}.tar.gz/g" README.md

./run_tests.sh >/tmp/ditto-release-tests.log 2>&1 || { echo "tests failed, see /tmp/ditto-release-tests.log" >&2; git checkout -- .; exit 1; }
git commit -qam "Release v$VER"
git tag -a "v$VER" -m "${NOTES:-Ditto for Linux $VER}"
git push -q origin main "v$VER"
echo "Pushed v$VER. GitHub Actions is building and publishing it:"
echo "  https://github.com/$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo IW0127/ditto-linux)/actions"
