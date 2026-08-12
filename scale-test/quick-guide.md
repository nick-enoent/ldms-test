# Quick Guide to resolve_cluster_yaml.py + flap_groups.py
# using submit_cluster_test.sh or run_standalone.sh

## 1. What each script is

- **`resolve_cluster_yaml.py`** -- Fills in a reusable YAML template with
  real hostnames, producing a complete cluster config that `ldmsd_yaml_parser`
  (and `scale_test.py -y`) can use directly.
- **`flap_groups.py`** -- Builds the flap spec string from the real daemon
  names in a resolved cluster config, so you don't have to type out daemon
  names by hand.
- **`submit_scale_test.sh`** -- The SLURM batch script that runs the two
  scripts above and then launches `scale_test.py` with their output.
- **`run_standalone.sh`** -- The same three-step pipeline as
  `submit_scale_test.sh`, for running a test without SLURM at all, on
  whatever machines you name directly.

Both `resolve_cluster_yaml.py` and `flap_groups.py` import `ldmsd.hostlist`,
already part of the ldmsd package you have installed, so there's nothing
extra to send you for that.

Both scripts can be used two ways: as standalone command-line scripts
(what `submit_scale_test.sh` calls), or as plain Python modules you can
import directly into your own code. Both are covered below.

## 2. Walking through submit_scale_test.sh

The batch script runs three steps in sequence.

### 2.1 Generate the complete YAML config

```bash
python3 resolve_cluster_yaml.py --template "${TEMPLATE}" --out "${RESOLVED_YAML}"
```

A template is an otherwise-normal ldmsd cluster YAML. Whoever writes or
modifies one needs to follow these rules:

- Daemon names must be prefixed with one of `sampler`, `l1`, `l2`, `l3`
  (case-insensitive). This is what lets `flap_groups.py` later figure out
  which daemons belong to which tier.
- `hosts:` must be left blank. The script fills it in with real node names.
- `names:` must already be fully written, including the count. For
  example, `names: &sampler "sampler-[1-16]"` means 16 daemons. This is
  the only place daemon counts come from -- there's no separate count
  option.
- `ports:` may be left blank or given explicitly. If blank, every daemon
  gets the same default port, `10001`. If given, your value is used as-is
  and left untouched.
- `path:` may be left blank or given explicitly, and can appear anywhere
  in the template, not just under `daemons:` (for example, under a
  plugin's `config:` list, like `store_sos`'s path). If blank, it's
  filled with `$SCRATCH/ldmsd-test/<run_id>` if `$SCRATCH` is set in the
  environment, otherwise `~/var/ldmsd/test/<run_id>`. `<run_id>` is the
  SLURM job ID when running under `sbatch`/`srun`, or a timestamp
  otherwise. If given explicitly, your value is used as-is. You can also
  force a specific path regardless of the template with `--path-override
  <path>` on the command line (or `path_override=` as a module argument),
  which only affects fields that were left blank -- it never overwrites
  something you wrote explicitly.
  **Every blank `path:` in the template resolves to the same value.**
  Confirmed by testing: a template with two different store plugins
  (`store_sos` and `store_csv`), both with a blank `path:`, both got the
  identical directory. If you ever have more than one store plugin active
  with a blank path, they'll be pointed at the same directory, which
  could collide depending on how those plugins name their own output
  files. Give at least one of them an explicit `path:` if that's a
  concern for a given template.

For getting the node list, you have three options:

- Run it inside an active SLURM allocation and give neither `--hostlist`
  nor `--nodefile`. It reads the real node list from `$SLURM_JOB_NODELIST`
  automatically. This is what the batch script does.
- `--hostlist "node-[1-20]"` -- an explicit hostlist string, for
  standalone runs outside SLURM.
- `--nodefile names.txt` -- a plain text file, one hostname per line, if
  your node list doesn't fit hostlist bracket syntax cleanly.

**Node count:** you need at least as many real hosts as daemons in the
template. Too few is a hard error. Extra hosts beyond what's needed are
fine -- they just go unused, and the script prints an info line listing
which ones, rather than failing.

Anything else already written explicitly in the template, like a literal
port or path, is left untouched.

The output is a complete YAML file, ready for `ldmsd_yaml_parser` or
`scale_test.py -y` as-is. It also prints a summary of which real hosts
were assigned to which daemon group.

### 2.2 Build the flap spec string

```bash
python3 flap_groups.py --from-yaml "${RESOLVED_YAML}" \
    --flap tier=sampler,flap_dur=30s,gap_dur=10s
```

This reads the real daemon names out of the resolved YAML and builds the
flap spec string in the format:

```
<daemon name/group>:<flap_dur>:<gap_dur>,<daemon name/group>:...
```

`--flap` is repeatable -- use one per tier you want to flap. Each one
takes key=value fields:

- `tier=` -- must match a daemon-name prefix. Valid values are `sampler`,
  `l1`, `l2`, `l3` (case-insensitive).
- `flap_dur=` -- how long those daemons stay running before being killed.
- `gap_dur=` -- how long they stay down before restarting.
- `count=` -- optional. Omit it to flap every daemon in that tier, or
  give a number to flap a subset (the first N by daemon number).

### 2.3 Run scale_test.py

```bash
python3 scale_test.py -y "${RESOLVED_YAML}" --FLAP_SPEC "${FLAP_STRING}"
```

`-y` is your existing, real flag. `--FLAP_SPEC` is **presumed** -- it
doesn't exist in the version of `scale_test.py` I have. This is the piece
that needs adding on your end: a flag that accepts the flap-string format
from step 2 and drives per-group flap/gap timing, replacing or living
alongside the current global `-S`/`-G` single-integer behavior.

### 2.4 One more thing about submitting this via sbatch

`sbatch` runs a stored copy of the script from SLURM's own spool
directory, not the original file. That means the script can't find its
own directory the usual way (via `$0`/`BASH_SOURCE`) to locate the other
files it needs. The batch script works around this with
`$SLURM_SUBMIT_DIR`, a SLURM-provided variable holding the directory the
job was submitted from. This assumes you run `sbatch submit_scale_test.sh`
from the same directory all the other files live in -- if you ever submit
by full path from somewhere else, that assumption breaks.

## 3. Running without SLURM -- run_standalone.sh

Same three-step pipeline as `submit_scale_test.sh`, driven by real
command-line flags instead of an `#SBATCH` header, since there's no
scheduler here to configure declaratively:

```bash
./run_standalone.sh --template cluster.yaml --hostlist "node-[1-20]" \
    --flap tier=sampler,flap_dur=30s,gap_dur=10s \
    --flap tier=l1,flap_dur=120s,gap_dur=20s
```

- `--template` is required.
- Exactly one of `--hostlist` or `--nodefile` is required. Unlike
  `resolve_cluster_yaml.py` on its own, this script won't silently try to
  fall back to SLURM if you give neither -- it's meant for the
  no-SLURM case, so it errors immediately and tells you to pick one.
- `--flap` works exactly like the flag of the same name in
  `flap_groups.py` -- repeatable, one per tier, at least one required.
- `--out` is optional. If you don't give it, the resolved YAML is
  written to `resolved-<timestamp>.yaml` in the current directory.

This script doesn't use `$SLURM_SUBMIT_DIR` at all, since it isn't run
through `sbatch` -- it finds its own directory the normal way, via
`$0`/`BASH_SOURCE`, which works fine here because nothing is copying the
script to a spool directory first.

`--FLAP_SPEC` on the final `scale_test.py` call is the same presumed flag
as in `submit_scale_test.sh` -- see section 2.3.

## 4. You don't have to run three separate scripts

Both modules are plain Python, so there's no need to shell out to them if
you're already writing Python. You can import them directly -- for
example, right inside `scale_test.py`'s own setup code, or in a small
wrapper of your own.

### 4.1 Generate the complete YAML config, as a module call

```python
from resolve_cluster_yaml import resolve_template

with open('cluster-template.yaml') as f:
    template_text = f.read()

# nodes=None pulls from $SLURM_JOB_NODELIST automatically inside an
# allocation. Pass nodes=[...] yourself to skip SLURM entirely.
resolved_text, info = resolve_template(template_text)

with open('resolved.yaml', 'w') as f:
    f.write(resolved_text)
```

- `info['groups']` gives you `{names_spec: {'hosts': [...], 'count': int}}`
  -- which real hosts ended up in which daemon group, if you want that
  for logging without re-reading the file.
- `info['unused_hosts']` lists any real hosts that weren't assigned to
  any daemon group, if you gave more nodes than needed.
- `info['run_id']` is whatever got used for default path resolution --
  the SLURM job ID under `sbatch`/`srun`, or a timestamp otherwise.

### 4.2 Get the flap spec string, as a module call

```python
from flap_groups import daemon_names_from_yaml, build_flap_daemon_groups, flap_chunks_to_string

daemon_names = daemon_names_from_yaml('resolved.yaml')

specs = [
    {'tier': 'sampler', 'flap_dur': '30s', 'gap_dur': '10s'},
    {'tier': 'l1', 'flap_dur': '120s', 'gap_dur': '20s', 'count': 2},
]
groups = build_flap_daemon_groups(specs, daemon_names)
# -> [{'daemons': 'sampler-[1-16]', 'flap_dur': '30s', 'gap_dur': '10s'},
#     {'daemons': 'l1-[1-2]', 'flap_dur': '120s', 'gap_dur': '20s'}]

flap_string = flap_chunks_to_string(groups)
# -> 'sampler-[1-16]:30s:10s,l1-[1-2]:120s:20s'
```

`build_flap_daemon_groups()` gives you a structured list, which is
probably easier to work with in Python than parsing the string back
apart. `flap_chunks_to_string()` turns that list into the exact string
format for `--FLAP_SPEC`, once that flag exists.

`daemon_names_from_yaml()` just reads `names:` fields out of the YAML
directly. It doesn't depend on `ldmsd_yaml_parser`/`parser_util`, since
that's kept deliberately lightweight. If you already have the daemon
names from somewhere else in `scale_test.py`, such as
`self.daemons.keys()` once the config is loaded, you can skip that call
entirely and pass your own list straight into `build_flap_daemon_groups()`.
It just needs a flat iterable of real daemon names and doesn't care where
they came from.

### 4.3 Error handling

Both `resolve_template()` and `build_flap_daemon_groups()` raise a plain
`ValueError` or `RuntimeError` with a clear message on bad input, such as
not enough nodes, an unknown tier, or a count that's too high. If you
want a clean printed message instead of a Python traceback, wrap the call
in a try/except, the same way the command-line versions of these scripts
already do.
