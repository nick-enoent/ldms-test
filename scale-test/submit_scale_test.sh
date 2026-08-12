#!/bin/bash
#
# submit_scale_test.sh -- SLURM batch script:
#   1. resolve_cluster_yaml.py: template + allocated nodes -> complete YAML
#   2. flap_groups.py: complete YAML + flap spec -> flap string
#   3. scale_test.py: complete YAML + flap string -> run the test
#
# ==========================================================================
# FIXME/TODO -- fill in / confirm before use:
#   - #SBATCH resource requests below (partition, account, node count, time
#     limit) are placeholders -- site- and allocation-specific.
#   - TEMPLATE / --flap group(s) & durations below are placeholders --
#     your call per run.
#   - SCRIPT_DIR assumes resolve_cluster_yaml.py, flap_groups.py,
#     hostlist.py, and scale_test.py are all in the same directory.
#   - --FLAP_SPEC below is a PRESUMED flag name, per your instruction to
#     assume one rather than use -S/-G. The scale_test.py I have does not
#     define this flag yet -- confirm the real name (and that per-group
#     flap parsing has actually been added) with Nick before relying on
#     this to run as-is.
#
# IMPORTANT -- findings from actually reading the scale_test.py I was
# given, not assumed:
#   - `from IPython.core.debugger import set_trace` at module level will
#     likely crash the job immediately if IPython isn't installed on the
#     compute node -- confirm it's available, or ask Nick to guard/remove
#     that import, before relying on this job actually starting.
#   - As currently written, -S/--FLAP_DUR does not appear to control an
#     actual sleep duration -- GAP_DUR governs both up-time and down-time
#     sleeps; FLAP_DUR only affects total elapsed-time accounting in the
#     loop. Since this script is bypassing -S/-G entirely in favor of the
#     presumed --FLAP_SPEC flag, this only matters once Nick's real
#     implementation of --FLAP_SPEC exists -- worth flagging to him now
#     regardless, since it's a real oddity in the current global-flap path.
# ==========================================================================

#SBATCH --job-name=ldms-scale-test
#SBATCH --nodes=2
#SBATCH --time=01:00:00
#SBATCH --output=scale-test-%j.out
#SBATCH --error=scale-test-%j.err

set -euo pipefail

# Under `sbatch`, SLURM executes a stored COPY of this script from its
# spool directory -- ${BASH_SOURCE[0]} then points at that spool copy,
# not the original file location, so it can't be used to find sibling
# files (resolve_cluster_yaml.py, flap_groups.py, scale_test.py, the
# template). $SLURM_SUBMIT_DIR is SLURM's own answer to this: the
# directory the job was submitted FROM. Falls back to BASH_SOURCE-based
# detection when not running under sbatch at all (e.g. testing locally).
# This assumes you submit from the same directory all those files live
# in -- if you instead run `sbatch /some/other/path/submit_scale_test.sh`
# from elsewhere, SLURM_SUBMIT_DIR won't be that script's directory
# either, and this would need to become an explicit path instead.
if [ -n "${SLURM_SUBMIT_DIR:-}" ]; then
    SCRIPT_DIR="${SLURM_SUBMIT_DIR}"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
cd "${SCRIPT_DIR}"

TEMPLATE="templates/mon-store-template.yaml"          # TODO: point at your actual template
RESOLVED_YAML="mon-test-run-${SLURM_JOB_ID}.yaml"  # TODO: Change to the name you want

# --- Step 1: resolve the template against this job's real allocated nodes.
# No --hostlist/--nodefile -- inside an active SLURM allocation,
# resolve_cluster_yaml.py pulls the node list from $SLURM_JOB_NODELIST
# itself (via `scontrol show hostnames`), which is already set here.
python3 resolve_cluster_yaml.py \
    --template "${TEMPLATE}" \
    --out "${RESOLVED_YAML}"

# --- Step 2: build the flap string from the resolved YAML's real daemon
# names. TODO: adjust --flap group(s)/durations to this run's actual plan.
FLAP_STRING="$(python3 flap_groups.py \
    --from-yaml "${RESOLVED_YAML}" \
    --flap tier=sampler,flap_dur=30s,gap_dur=10s)" # TODO: Modify the --flap option

echo "Resolved YAML: ${RESOLVED_YAML}"
echo "Flap string:   ${FLAP_STRING}"

# --- Step 3: run scale_test.py.
# Deliberately NOT launched via srun/mpirun: scale_test.py runs as a single
# process on this job's launch node and reaches the other allocated nodes
# itself via ssh (Popen(ssh ...) internally), per its own design -- the
# #SBATCH --nodes above just reserves the allocation for it to use by
# hostname, it doesn't distribute this script's own execution across them.
#
# -y/--yaml_file is confirmed (required, the complete resolved YAML).
# --FLAP_SPEC is PRESUMED -- see IMPORTANT note above.
SCALE_TEST_FLAP_FLAG="--FLAP_SPEC"   # FIXME : Change to the actual scale_test cmd-line option

#python3 scale_test.py \
#    -y "${RESOLVED_YAML}" \
#    "${SCALE_TEST_FLAP_FLAG}" "${FLAP_STRING}"
python3 scale_test.py \
    -y "${RESOLVED_YAML}"
