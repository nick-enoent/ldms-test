#!/bin/bash
#
# run_standalone.sh -- run a scale test without SLURM, on an explicit set
# of machines you provide directly. Same three-step pipeline as
# submit_scale_test.sh (resolve_cluster_yaml.py -> flap_groups.py ->
# scale_test.py), just driven by real command-line flags instead of an
# #SBATCH header, since there's no scheduler here to configure declaratively.
#
# Usage:
#   ./run_standalone.sh --template cluster.yaml --hostlist "node-[1-20]" \
#       --flap tier=sampler,flap_dur=30s,gap_dur=10s \
#       --flap tier=l1,flap_dur=120s,gap_dur=20s
#
#   ./run_standalone.sh --template cluster.yaml --nodefile names.txt \
#       --flap tier=sampler,flap_dur=30s,gap_dur=10s
#
# --FLAP_SPEC on the final scale_test.py call is PRESUMED, same as in
# submit_scale_test.sh -- see the guide for Nick for details.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: run_standalone.sh --template <path> (--hostlist <str> | --nodefile <path>)
                          --flap <spec> [--flap <spec> ...] [--out <path>]

  --template <path>    Path to the template YAML.
  --hostlist <str>     Explicit hostlist string, e.g. "node-[1-20]".
  --nodefile <path>    Plain text file, one real hostname per line.
                        Exactly one of --hostlist/--nodefile is required.
  --flap <spec>        tier=<name>,flap_dur=<dur>,gap_dur=<dur>[,count=<n>]
                        Repeatable, one per tier. At least one required.
  --out <path>         Where to write the resolved YAML. Defaults to
                        resolved-<timestamp>.yaml if not given.
  -h, --help           Show this message.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

TEMPLATE=""
HOSTLIST=""
NODEFILE=""
OUT=""
FLAP_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --template) TEMPLATE="$2"; shift 2 ;;
        --hostlist) HOSTLIST="$2"; shift 2 ;;
        --nodefile) NODEFILE="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --flap) FLAP_ARGS+=("--flap" "$2"); shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "run_standalone.sh: unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

if [ -z "${TEMPLATE}" ]; then
    echo "run_standalone.sh: --template is required" >&2; usage >&2; exit 1
fi
if [ -n "${HOSTLIST}" ] && [ -n "${NODEFILE}" ]; then
    echo "run_standalone.sh: --hostlist and --nodefile are mutually exclusive" >&2; exit 1
fi
if [ -z "${HOSTLIST}" ] && [ -z "${NODEFILE}" ]; then
    echo "run_standalone.sh: one of --hostlist or --nodefile is required" \
         "(this is the standalone, non-SLURM path -- there's no allocation" \
         "to pull a node list from automatically)" >&2
    exit 1
fi
if [ "${#FLAP_ARGS[@]}" -eq 0 ]; then
    echo "run_standalone.sh: at least one --flap is required" >&2; usage >&2; exit 1
fi

if [ -z "${OUT}" ]; then
    OUT="resolved-$(date +%Y%m%d-%H%M%S).yaml"
fi

NODE_SOURCE_ARGS=()
if [ -n "${HOSTLIST}" ]; then
    NODE_SOURCE_ARGS=(--hostlist "${HOSTLIST}")
else
    NODE_SOURCE_ARGS=(--nodefile "${NODEFILE}")
fi

# --- Step 1: resolve the template against the given nodes.
python3 resolve_cluster_yaml.py \
    --template "${TEMPLATE}" \
    --out "${OUT}" \
    "${NODE_SOURCE_ARGS[@]}"

# --- Step 2: build the flap string from the resolved YAML's real daemon names.
FLAP_STRING="$(python3 flap_groups.py --from-yaml "${OUT}" "${FLAP_ARGS[@]}")"

echo "Resolved YAML: ${OUT}"
echo "Flap string:   ${FLAP_STRING}"

# --- Step 3: run scale_test.py. Runs directly on this machine -- no ssh
# orchestration layer here beyond what scale_test.py itself does internally
# to reach the hosts you gave via --hostlist/--nodefile.
SCALE_TEST_FLAP_FLAG="--FLAP_SPEC"   # PRESUMED -- see submit_scale_test.sh

python3 scale_test.py \
    -y "${OUT}" \
    "${SCALE_TEST_FLAP_FLAG}" "${FLAP_STRING}"
