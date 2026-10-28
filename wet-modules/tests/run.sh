#!/usr/bin/env bash
# Runs the wet modules' nf-tests against a fake-backend adapter started here (port 8190, a throwaway
# ledger). Needs `make install` (the nf-lab plugin) and `uv sync` in adapter/. Arguments go to nf-test.
#   wet-modules/tests/run.sh [nf-test test args: a path, --tag, --update-snapshot; default: everything]
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${NF_LAB_TEST_PORT:-8190}
TMP=$(mktemp -d)
LEDGER=$TMP/ledger.sqlite
ln -s "$PWD/../adapter/src/nf_lab_adapter/labctl.py" "$TMP/labctl"  # the examples do this in their bin/
(cd ../adapter && NF_LAB_BACKEND=fake NF_LAB_DEVICES=liquid-handling:2,plate-reading:2,thermocycling:2,qpcr:2,\
shaking:2,heating-shaking:2,temperature-control:2,tilting:2,centrifugation:2,incubation:2,\
pumping:2,weighing:2,powder-dispensing:2,sealing:2,peeling:2,barcode-reading:2 \
    NF_LAB_STEP=0.05 NF_LAB_LEDGER=$LEDGER exec uv run uvicorn nf_lab_adapter.app:main --factory --port "$PORT") \
    >"$TMP/adapter.log" 2>&1 &
ADAPTER=$!
trap 'kill $ADAPTER' EXIT
for _ in $(seq 50); do curl -sf "http://127.0.0.1:$PORT/devices" >/dev/null && break; sleep 0.2; done
PATH=$TMP:$PATH NF_LAB_ADAPTER=http://127.0.0.1:$PORT ${NF_TEST:-nf-test} test "${@:-.}"
