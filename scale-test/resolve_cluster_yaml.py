#!/usr/bin/env python3
"""
resolve_cluster_yaml.py

Fills in an ldmsd cluster YAML *template* with real hostnames, producing
a complete YAML file ready for ldmsd_yaml_parser.

USAGE AS A SCRIPT
------------------
Standalone (explicit hostlist, no SLURM):

    python3 resolve_cluster_yaml.py \\
        --template cluster.yaml \\
        --out resolved.yaml \\
        --hostlist "node-[1-20]"

Inside a SLURM allocation (omit --hostlist; reads $SLURM_JOB_NODELIST):

    python3 resolve_cluster_yaml.py \\
        --template cluster.yaml \\
        --out resolved.yaml

Run `python3 resolve_cluster_yaml.py --help` for the full option list.

USAGE AS A MODULE
------------------
    from resolve_cluster_yaml import resolve_template

    with open('cluster.yaml') as f:
        template_text = f.read()

    # nodes=None pulls from SLURM; pass a list to supply hosts directly
    resolved_text, info = resolve_template(template_text, nodes=nodes)

    info['groups']   # {names_spec: {'hosts': [...], 'count': int}}
    info['run_id']   # run id used for default path resolution -- the
                      # SLURM job ID when running under sbatch/srun, or a
                      # timestamp otherwise

TEMPLATE FORMAT
------------------
A template is a normal, otherwise-valid ldmsd_yaml_parser YAML file with
three fields left as tester choices:

  - `names:`  MUST be fully written, including the count, e.g.
              `names: &samplerd "samplerd-[1-16]"` (16 daemons). This is
              the only place daemon counts come from -- there is no
              separate count parameter.
  - `hosts:`  MUST be left blank. This script fills it in with real
              node names.
  - `ports:` / `path:`  MAY be left blank (filled from a default) or
              given explicitly (left untouched, tester's value wins).

No template-only syntax is introduced -- a template is valid
ldmsd_yaml_parser YAML with some values missing, nothing more. Aliases
(`*samplerd`, `*l1`, ...) work exactly as normal YAML, since only
`hosts:` (never aliased anywhere in this schema) is generated -- fields
that other parts of the document reference (`names:`) are never touched,
so plain `yaml.safe_load()` is enough; no anchor/alias-graph manipulation
needed.

DESIGN NOTES
------------------
  - No daemon<->host pairing is computed or returned here. That's the
    job of whatever loads the resolved YAML through the real
    ldmsd_yaml_parser (scale_test.py already does this) -- duplicating
    build_daemons()'s internal pairing logic here would be a second copy
    that could silently drift if that logic ever changes.
  - group_counts_override (below) is a placeholder for a future
    CLI-supplied-count option; not implemented yet.
"""
import os
import time
import yaml
import ldmsd.hostlist as hostlist

DEFAULT_PORT_RANGE = (10001, 10500)


# --------------------------------------------------------------------------
# Node sourcing / chunking
# --------------------------------------------------------------------------

def get_slurm_nodelist(nodelist_env="SLURM_JOB_NODELIST"):
    """Return the expanded list of real hostnames in the current SLURM
    allocation, using `scontrol show hostnames`.

    Raises RuntimeError if the env var is unset or scontrol fails -- fail
    loudly rather than silently falling back to something wrong.
    """
    raw = os.environ.get(nodelist_env)
    if not raw:
        raise RuntimeError(
            f'${nodelist_env} is not set -- is this running inside an '
            f'active SLURM allocation (salloc/sbatch)?'
        )
    import subprocess
    try:
        out = subprocess.check_output(["scontrol", "show", "hostnames", raw], text=True)
    except FileNotFoundError:
        raise RuntimeError(
            'scontrol not found on PATH -- must run on a node with SLURM '
            'client tools installed.'
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f'scontrol failed on nodelist "{raw}": {e}')
    nodes = [n for n in out.splitlines() if n.strip()]
    if not nodes:
        raise RuntimeError(f'scontrol returned no hostnames for "{raw}"')
    return nodes


def chunk_nodes(nodes, role_counts):
    """Slice `nodes` into named chunks, in the order given.

    role_counts: ordered list of (label, count) pairs. count of -1 means
                 "take everything left".

    Raises ValueError if there aren't enough nodes for a role -- that's
    unavoidable. Leftover nodes after all roles are assigned are fine
    ("at least" semantics) -- the caller can inspect what's left over via
    the second return value and report it however it likes (this function
    doesn't print anything itself).

    Returns: (assigned, leftover) -- assigned is dict label -> list of
    nodes; leftover is whatever wasn't consumed by any role.
    """
    remaining = list(nodes)
    assigned = {}
    for role, count in role_counts:
        if count == -1:
            count = len(remaining)
        if count > len(remaining):
            raise ValueError(
                f'Not enough nodes for "{role}": need {count}, '
                f'{len(remaining)} left (allocation has {len(nodes)} total)'
            )
        assigned[role] = remaining[:count]
        remaining = remaining[count:]
    return assigned, remaining


def role_hostlist_str(hosts):
    """Compress a group's real hostnames into hostlist shorthand."""
    if not hosts:
        return ""
    return hostlist.collect_hostlist(hosts)


# --------------------------------------------------------------------------
# Leaf-attribute defaults (ports / path) -- only used when blank
# --------------------------------------------------------------------------

def default_scratch_path(run_id):
    """Default scratch directory for a test run. Prefers $SCRATCH over
    $HOME (not guaranteed shared across nodes on bare metal); falls back
    to ~/var/ldmsd/test/<run_id>. Assumed, not yet confirmed with Nick/Tom.
    """
    scratch = os.environ.get('SCRATCH')
    if scratch:
        return os.path.join(scratch, 'ldmsd-test', run_id)
    return os.path.expanduser(f'~/var/ldmsd/test/{run_id}')


# --------------------------------------------------------------------------
# Template resolution
# --------------------------------------------------------------------------

def _fill_blank(node, key, value):
    """Set node[key] = value only if it's currently None (blank in the
    template). An explicit value already in the template is left alone.
    """
    if node.get(key) is None:
        node[key] = value


def resolve_template(template_text, nodes=None, port_range=DEFAULT_PORT_RANGE,
                      run_id=None, path_override=None,
                      group_counts_override=None):
    """Resolve a template into concrete ldmsd_yaml_parser input.

    nodes: real hostnames to place daemons on. If None, pulled from SLURM.
        At least as many nodes as daemons are required; extra unused
        nodes are fine (see info['unused_hosts'] below), not an error.
    group_counts_override: NOT YET IMPLEMENTED -- extension point for
        supplying counts via CLI instead of deriving them from each
        group's `names:`. When added, this should override the count
        derived in step 2 for whichever group(s) it names, rather than
        replacing the derivation entirely.

    Returns (resolved_yaml_text, info) where info has:
      'groups': {names_spec: {'hosts': [...], 'count': int}}
      'unused_hosts': list of real hosts not assigned to any group
      'run_id': the run_id used for path resolution
    """
    if group_counts_override is not None:
        raise NotImplementedError(
            'group_counts_override is a planned extension point, not yet '
            'implemented -- counts are always derived from each group\'s '
            '`names:` for now.'
        )

    if nodes is None:
        nodes = get_slurm_nodelist()
    if run_id is None:
        run_id = os.environ.get('SLURM_JOB_ID') or time.strftime('%Y%m%d-%H%M%S')

    doc = yaml.safe_load(template_text)
    if 'daemons' not in doc or not isinstance(doc['daemons'], list):
        raise ValueError('Template has no top-level `daemons:` list')

    # Step 2: derive each group's count from its own (already-complete)
    # `names:` -- this is the only place the tester's counts live.
    role_counts = []
    for grp in doc['daemons']:
        if 'names' not in grp or grp['names'] is None:
            raise ValueError(
                f'Template `daemons:` entry has no `names:` -- unlike '
                f'`hosts:`, `names:` must be fully written by the '
                f'template author: {grp}'
            )
        count = len(hostlist.expand_hostlist(grp['names']))
        role_counts.append((grp['names'], count))

    # Step 3: chunk real nodes across groups, in template order. Extra
    # nodes beyond what's needed are fine -- "at least" semantics.
    chunks, unused_hosts = chunk_nodes(nodes, role_counts)

    groups_info = {}
    base_port = port_range[0]

    for grp in doc['daemons']:
        names_spec = grp['names']
        hosts = chunks[names_spec]
        hosts_str = role_hostlist_str(hosts)

        # Step 4: fill hosts only if blank
        _fill_blank(grp, 'hosts', hosts_str)

        # Step 5: fill ports/path only if blank, anywhere they appear
        # inside this group (endpoints list, and any nested plugin config
        # -- but plugin config lives under top-level `plugins:`, handled
        # separately below since it's not nested under `daemons:`).
        for ep in grp.get('endpoints', []):
            _fill_blank(ep, 'ports', str(base_port))

        groups_info[names_spec] = {'hosts': hosts, 'count': len(hosts)}

    # path: blanks can live anywhere (e.g. plugins.*.config[].path) --
    # generic leaf fill, not scoped to `daemons:`
    def fill_paths(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == 'path' and v is None:
                    node[k] = path_override if path_override else default_scratch_path(run_id)
                else:
                    fill_paths(v)
        elif isinstance(node, list):
            for item in node:
                fill_paths(item)
    fill_paths(doc)

    resolved_text = yaml.dump(doc, sort_keys=False)
    return resolved_text, {'groups': groups_info, 'unused_hosts': unused_hosts, 'run_id': run_id}


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='Fill an ldmsd cluster YAML template with real hostnames.',
        epilog='Example:\n'
               '  python3 resolve_cluster_yaml.py --template cluster.yaml '
               '--out resolved.yaml --hostlist "node-[1-20]"\n'
               '  (omit --hostlist to pull nodes from $SLURM_JOB_NODELIST instead)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--template', required=True,
                     help='Path to the template YAML (see TEMPLATE FORMAT in '
                          'this file\'s module docstring).')
    ap.add_argument('--out', required=True,
                     help='Path to write the resolved, complete YAML to.')
    ap.add_argument('--hostlist', default=None,
                     help='Explicit hostlist string for standalone (non-SLURM) '
                          'runs, e.g. "node-[1-20]" or "node-[1-5,12],other-3". '
                          'Non-contiguous lists are fine -- see hostlist.py '
                          'syntax. Mutually exclusive with --nodefile. If '
                          'neither is given, nodes are pulled from '
                          '$SLURM_JOB_NODELIST.')
    ap.add_argument('--nodefile', default=None,
                     help='Path to a plain text file, one real hostname per '
                          'line (blank lines and #-comments ignored) -- for '
                          'irregular node lists where writing correct '
                          'hostlist bracket syntax by hand is inconvenient. '
                          'Mutually exclusive with --hostlist.')
    ap.add_argument('--path-override', default=None,
                     help='Value to use for any blank `path:` field in the '
                          'template, instead of the default scratch-dir '
                          'convention ($SCRATCH/ldmsd-test/<run_id> or '
                          '~/var/ldmsd/test/<run_id>).')
    args = ap.parse_args()

    if args.hostlist and args.nodefile:
        ap.error('--hostlist and --nodefile are mutually exclusive')

    if args.nodefile:
        with open(args.nodefile) as f:
            nodes = [line.split('#', 1)[0].strip() for line in f]
        nodes = [n for n in nodes if n]
        if not nodes:
            ap.error(f'--nodefile {args.nodefile!r} contained no hostnames')
    elif args.hostlist:
        nodes = hostlist.expand_hostlist(args.hostlist)
    else:
        nodes = None

    with open(args.template) as f:
        template_text = f.read()

    resolved_text, info = resolve_template(
        template_text, nodes=nodes, path_override=args.path_override)

    with open(args.out, 'w') as f:
        f.write(resolved_text)

    print(f'# Resolved YAML written to {args.out}')
    print(f'# run_id: {info["run_id"]}')
    for names_spec, g in info['groups'].items():
        print(f'#   group {names_spec!r}: {g["count"]} daemon(s) on {g["hosts"]}')
    if info['unused_hosts']:
        print(f'# info: {len(info["unused_hosts"])} allocated node(s) not used '
              f'by any daemon group: {info["unused_hosts"]}')


if __name__ == '__main__':
    import sys
    # Errors we deliberately raise for expected/anticipated problems (bad
    # counts, missing files, malformed template, etc.) print as a clean
    # one-line message here instead of a traceback -- that's the CLI
    # contract. Anything NOT in this list is an actual bug in the script
    # and should still show a full traceback, not be hidden.
    try:
        main()
    except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError,
            yaml.YAMLError) as e:
        print(f'resolve_cluster_yaml.py: error: {e}', file=sys.stderr)
        sys.exit(1)
