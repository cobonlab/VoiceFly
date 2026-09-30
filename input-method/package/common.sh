#!/bin/bash

PROJECT_ROOT="$(cd "$(dirname "$BASH_SOURCE")/.."; pwd)"

resolve_derived_data_path() {
    local candidate="${1-}"
    if [ -z "${candidate}" ]; then
        echo >&2 'ERROR: DERIVED_DATA_PATH must not be empty.'
        return 64
    fi

    case "${candidate}" in
        /*) ;;
        *) candidate="${PROJECT_ROOT}/${candidate}" ;;
    esac

    if [ "${candidate}" = / ]; then
        echo >&2 'ERROR: refusing to use the filesystem root as DERIVED_DATA_PATH.'
        return 64
    fi
    printf '%s\n' "${candidate%/}"
}

bump_version() {
    local version="$1"
    cd "${PROJECT_ROOT}"
    xcrun agvtool new-version "${version}"
}

get_app_version() {
    cd "${PROJECT_ROOT}"
    xcrun agvtool what-version | sed -n 'n;s/^[[:space:]]*\([0-9.]*\)$/\1/;p'
}

# deprecated
get_bundle_version() {
    sed -n '/CFBundleVersion/{n;s/.*<string>\(.*\)<\/string>.*/\1/;p;}' "$@"
}

match_line() {
    grep --quiet --fixed-strings "$@"
}
