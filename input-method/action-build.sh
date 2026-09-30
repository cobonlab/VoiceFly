#!/usr/bin/env bash

set -e

target="${1:-release}"

# VoiceFlypy currently ships an arm64-only main executable. Keep the build
# contract aligned with the installer declaration until universal output is
# produced and verified end to end.
export ARCHS='arm64'

export SQUIRREL_BUNDLED_RECIPES='
  lotem/rime-octagram-data
  lotem/rime-octagram-data@hant
'

# preinstall
./action-install.sh

# build dependencies
# make deps

# build Squirrel
make "${target}"

echo 'Installer package:'
find package -type f -name '*.pkg' -or -name '*.zip'
