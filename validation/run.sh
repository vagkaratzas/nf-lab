#!/usr/bin/env bash
# End-to-end checks of the success criteria: ./run.sh [name ...] (default: all), in parallel.
# Needs the plugin installed (make install), the adapter's venv (cd adapter && uv sync)
# and nextflow on PATH (or NXF_CMD). Each check runs in build/validation/<name> against
# its own adapter, and fails after CHECK_TIMEOUT seconds (default 300). CHECK_JOBS caps how many
# run at once (default: all); a small CI runner needs it, or the timing checks (robots side by side) starve.
set -uo pipefail
VALIDATION=$(cd "$(dirname "$0")" && pwd)
SCRATCH=$VALIDATION/../build/validation
names=("$@"); [[ ${#names[@]} -gt 0 ]] || names=($(ls "$VALIDATION/checks"))

check() {
    local name=$1 dir=$SCRATCH/$1
    rm -rf "$dir"; mkdir -p "$dir"
    if (cd "$dir" && timeout "${CHECK_TIMEOUT:-300}" bash -x -c "source '$VALIDATION/lib.sh'; source '$VALIDATION/checks/$name/.checks'") > "$dir/checks.out" 2>&1; then
        echo "ok    $name"
    else
        echo "FAIL  $name ($dir/checks.out)"; return 1
    fi
}

export -f check; export VALIDATION SCRATCH
printf '%s\n' "${names[@]}" | xargs -P "${CHECK_JOBS:-0}" -I{} bash -c 'check {}'  # exits non-zero if any fails
