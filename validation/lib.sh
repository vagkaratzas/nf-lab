# Sourced by each checks/<name>/.checks, which runs in an empty scratch dir.
set -euo pipefail
VALIDATION=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ADAPTER_DIR=$VALIDATION/../adapter
export NXF_VER=${NXF_VER:-26.10.2}
NXF_CMD=${NXF_CMD:-nextflow}

# nf <args>: run the validation pipeline from the current dir
nf() { "$NXF_CMD" -q run "$VALIDATION" -ansi-log false "$@"; }
# nf_bg <args>: same, in the background; NF_PID is Nextflow itself, so a signal reaches it
nf_bg() { "$NXF_CMD" -q run "$VALIDATION" -ansi-log false "$@" & NF_PID=$!; }

# start_adapter: a FakeBackend adapter on a free port, ledger in ./ledger.sqlite.
# Pass settings as env, e.g. NF_LAB_STEP=1 start_adapter. Stopped when the check exits.
start_adapter() {
    local port
    port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
    NF_LAB_LEDGER=$PWD/ledger.sqlite NF_LAB_BACKEND=fake NF_LAB_STEP=${NF_LAB_STEP:-0.1} \
        "$ADAPTER_DIR/.venv/bin/python" -m uvicorn nf_lab_adapter.app:main --factory --port "$port" \
        --app-dir "$ADAPTER_DIR/src" > adapter.log 2>&1 &
    ADAPTER_PID=$!
    trap 'kill $ADAPTER_PID ${NF_PID:-} 2> /dev/null || true' EXIT
    trap 'exit 124' TERM  # a timeout: still run the EXIT trap
    export NF_LAB_ADAPTER=http://127.0.0.1:$port LEDGER=$PWD/ledger.sqlite
    until curl -sf -o /dev/null "$NF_LAB_ADAPTER/docs"; do sleep 0.2; done
}

# sql "<query>": rows of the ledger, columns joined by |
sql() {
    python3 -c 'import sqlite3, sys; [print("|".join(map(str, r))) for r in sqlite3.connect(sys.argv[1]).execute(sys.argv[2])]' \
        "$LEDGER" "$1"
}

# logged "<text>": how many lines of the last run's .nextflow.log contain it
logged() { grep -c -- "$1" .nextflow.log || true; }

# until_sql "<query>": wait (up to 30 s) until the query returns 1
until_sql() {
    for _ in $(seq 150); do [[ $(sql "$1") == 1 ]] && return; sleep 0.2; done
    echo "timed out waiting for: $1" >&2; return 1
}

# fails <cmd>: the command must fail (`! cmd` never stops a set -e script)
fails() { if "$@"; then echo "expected to fail: $*" >&2; return 1; fi; }
