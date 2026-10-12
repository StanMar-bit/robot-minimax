#!/bin/sh
# Install node_modules outside a synced folder (OneDrive, Dropbox, ...) and link it here.
# npm refuses a symlinked node_modules, so the install runs in a cache directory
# on a copy of package.json and package-lock.json.
set -e
cd "$(dirname "$0")"
CACHE="${MINIMAX_CACHE:-$HOME/.cache/robot-minimax}/web"
mkdir -p "$CACHE"
cp package.json package-lock.json "$CACHE"/
(cd "$CACHE" && npm ci --no-fund --no-audit)
rm -rf node_modules
ln -s "$CACHE/node_modules" node_modules
echo "node_modules -> $CACHE/node_modules"
