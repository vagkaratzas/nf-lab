#!/usr/bin/env bash
# End-to-end checks of the success criteria: ./run.sh [name ...] (default: all).
# Needs the plugin installed (make install), the adapter's venv (cd adapter && uv sync)
# and nextflow on PATH (or NXF_CMD). Each check runs in build/validation/<name>,
# and fails after CHECK_TIMEOUT seconds (default 300).
set -uo pipefail
VALIDATION=$(cd "$(dirname "$0")" && pwd)
SCRATCH=$VALIDATION/../build/validation
names=("$@"); [[ ${#names[@]} -gt 0 ]] || names=($(ls "$VALIDATION/checks"))
failed=()
for name in "${names[@]}"; do
    dir=$SCRATCH/$name; rm -rf "$dir"; mkdir -p "$dir"
    if (cd "$dir" && timeout "${CHECK_TIMEOUT:-300}" bash -x -c "source '$VALIDATION/lib.sh'; source '$VALIDATION/checks/$name/.checks'") > "$dir/checks.out" 2>&1; then
        echo "ok    $name"
    else
        echo "FAIL  $name ($dir/checks.out)"; failed+=("$name")
    fi
done
[[ ${#failed[@]} -eq 0 ]]
