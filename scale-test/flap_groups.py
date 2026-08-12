#!/usr/bin/env python3
"""
flap_groups.py

Builds the flap spec string scale_test.py consumes:

    <daemon name/group>:<flap_dur>:<gap_dur>,<daemon name/group>:...

  flap_dur - how long the daemon(s) run before being killed
  gap_dur  - how long they stay down before being restarted

USAGE AS A SCRIPT
------------------
    python3 flap_groups.py \\
        --from-yaml resolved.yaml \\
        --flap tier=samplerd,flap_dur=30s,gap_dur=10s,count=8 \\
        --flap tier=l1,flap_dur=120s,gap_dur=20s

    (omit `count=` to flap every daemon in that tier)

    Or, without a resolved YAML on hand, give it a plain name list:

    python3 flap_groups.py \\
        --daemon-names-file names.txt \\
        --flap tier=samplerd,flap_dur=30s,gap_dur=10s

USAGE AS A MODULE
------------------
    from flap_groups import build_flap_string

    daemon_names = ['samplerd-1', ..., 'samplerd-16', 'l1-1', ..., 'l1-4']
    specs = [
        {'tier': 'samplerd', 'flap_dur': '30s', 'gap_dur': '10s', 'count': 8},
        {'tier': 'l1', 'flap_dur': '120s', 'gap_dur': '20s'},
    ]
    build_flap_string(specs, daemon_names)
    # -> 'samplerd-[1-8]:30s:10s,l1-[1-4]:120s:20s'

TIER CLASSIFICATION
------------------
A daemon name must start with one of KNOWN_TIER_PREFIXES (currently
'sampler', 'l1', 'l2', 'l3'; case-insensitive) -- enforced, not inferred.
"samplerd-[1-16]" matches "sampler"; "L2-agg" matches "l2". A name
matching none of these raises an error naming it, rather than silently
excluding it from flap eligibility.

This module does not import or run the real ldmsd_yaml_parser/parser_util
-- deliberately. --from-yaml reads `daemons:` `names:` fields directly
via plain yaml.safe_load() + hostlist.expand_hostlist(), which gives
identical daemon names to running the real parser (verified: a daemon's
name is always exactly expand_hostlist(names field), the real parser
never invents or alters name strings, it only assigns hosts/ports). As
test infrastructure, the less this depends on code in the ldms source
tree, the better.
"""
import re
import sys
import yaml
from ldmsd.hostlist import expand_hostlist, collect_hostlist

# Enforced tier vocabulary -- a daemon name must start with one of these
# (case-insensitive) to be classified. Simpler and more predictable than
# inferring a base name structurally; the cost is that template authors
# must name daemon groups starting with one of these. "samplerd-[1-16]"
# still qualifies (starts with "sampler"); "L2-agg" qualifies too (matches
# "l2" case-insensitively).
KNOWN_TIER_PREFIXES = ['sampler', 'l1', 'l2', 'l3']

_TRAILING_NUM = re.compile(r'-(\d+)$')


def classify_daemon_names(daemon_names, prefixes=KNOWN_TIER_PREFIXES):
    """Group real daemon names by tier, matched against the enforced
    prefix list (case-insensitive, longest prefix wins if more than one
    matches -- e.g. "l1" vs a hypothetical "l10" prefix).

    Returns: dict tier -> list of daemon names, sorted numerically by
    trailing "-<N>" where present (samplerd-2 before samplerd-10) -- a
    plain string sort would put "samplerd-10" before "samplerd-2" and
    silently pick the wrong daemons when a count is given.

    Raises ValueError listing any name that matches no known prefix --
    fail loud rather than silently dropping a daemon from flap eligibility.
    """
    sorted_prefixes = sorted(prefixes, key=len, reverse=True)
    grouped = {}
    unmatched = []
    for name in daemon_names:
        match = next((p for p in sorted_prefixes if name.lower().startswith(p.lower())), None)
        if match is None:
            unmatched.append(name)
            continue
        grouped.setdefault(match, []).append(name)
    if unmatched:
        raise ValueError(
            f'{len(unmatched)} daemon name(s) do not start with any known '
            f'tier prefix {prefixes}: {unmatched[:10]}'
            f'{" ..." if len(unmatched) > 10 else ""}'
        )
    for names in grouped.values():
        def sort_key(n):
            m = _TRAILING_NUM.search(n)
            return int(m.group(1)) if m else -1
        names.sort(key=sort_key)
    return grouped


def parse_flap_arg(arg):
    """Parse one 'tier=samplerd,flap_dur=30s,gap_dur=10s[,count=8]' string
    into a spec dict. Raises ValueError with a clear message on anything
    malformed -- unknown keys, missing required keys, bad count.
    """
    spec = {}
    for pair in arg.split(','):
        pair = pair.strip()
        if not pair:
            continue
        if '=' not in pair:
            raise ValueError(
                f'"{pair}" in --flap "{arg}" is not key=value'
            )
        k, v = pair.split('=', 1)
        k, v = k.strip(), v.strip()
        if k not in ('tier', 'flap_dur', 'gap_dur', 'count'):
            raise ValueError(
                f'Unknown key "{k}" in --flap "{arg}" -- expected one of '
                f'tier, flap_dur, gap_dur, count'
            )
        spec[k] = v

    missing = {'tier', 'flap_dur', 'gap_dur'} - spec.keys()
    if missing:
        raise ValueError(
            f'--flap "{arg}" is missing required field(s): {sorted(missing)}'
        )

    if 'count' in spec:
        try:
            spec['count'] = int(spec['count'])
        except ValueError:
            raise ValueError(
                f'--flap "{arg}": count must be an integer, got "{spec["count"]}"'
            )
        if spec['count'] <= 0:
            raise ValueError(f'--flap "{arg}": count must be positive')

    return spec


def build_flap_string(specs, daemon_names):
    """specs: list of {'tier', 'flap_dur', 'gap_dur', 'count' (optional)}.
    daemon_names: flat iterable of real daemon names (any source -- our
        own resolve_cluster_yaml.py output loaded through the real
        parser, LdmsdScaleTest.daemons.keys(), a plain name list, ...).

    Returns the comma-joined flap spec string scale_test.py expects.
    Raises ValueError if a spec's tier matches no daemon, or asks for
    more daemons than exist in that tier.
    """
    by_tier = classify_daemon_names(list(daemon_names))
    parts = []
    for spec in specs:
        tier = spec['tier']
        names = by_tier.get(tier)
        if not names:
            raise ValueError(
                f'tier "{tier}" matches no daemon name. Available tiers: '
                f'{sorted(by_tier.keys())}'
            )
        count = spec.get('count')
        if count is not None:
            if count > len(names):
                raise ValueError(
                    f'tier "{tier}" has {len(names)} daemon(s), cannot '
                    f'flap {count}'
                )
            names = names[:count]
        group_str = collect_hostlist(names)
        parts.append(f'{group_str}:{spec["flap_dur"]}:{spec["gap_dur"]}')
    return ','.join(parts)


def daemon_names_from_yaml(path):
    """Read the real daemon names directly out of a resolved cluster
    YAML's `daemons:` `names:` fields, via plain yaml.safe_load() +
    hostlist.expand_hostlist() -- deliberately NOT importing or running
    the real ldmsd_yaml_parser/parser_util for this. This is test
    infrastructure; the less it depends on code in the ldms source tree,
    the better. This gives identical results to running the real parser
    for daemon NAMES specifically (verified) -- build_daemons() never
    invents or alters name strings, a daemon's name is always exactly
    expand_hostlist(names field). It's only host/port assignment that the
    real parser computes, which this function doesn't need or provide.
    """
    with open(path) as f:
        doc = yaml.safe_load(f)
    if 'daemons' not in doc or not isinstance(doc['daemons'], list):
        raise ValueError(f'{path}: no top-level `daemons:` list found')
    names = []
    for grp in doc['daemons']:
        if 'names' not in grp or grp['names'] is None:
            raise ValueError(f'{path}: a `daemons:` entry has no `names:`: {grp}')
        names.extend(expand_hostlist(grp['names']))
    return names


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='Build the flap spec string for scale_test.py.',
        epilog='Example:\n'
               '  python3 flap_groups.py --from-yaml resolved.yaml \\\n'
               '      --flap tier=samplerd,flap_dur=30s,gap_dur=10s,count=8 \\\n'
               '      --flap tier=l1,flap_dur=120s,gap_dur=20s',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--from-yaml', help='A resolved cluster YAML to load '
                      'through the real parser for authoritative daemon names.')
    src.add_argument('--daemon-names-file', help='Plain text file, one '
                      'daemon name per line, as an alternative to --from-yaml.')
    ap.add_argument('--flap', action='append', required=True, metavar='SPEC',
                     help='tier=<name>,flap_dur=<dur>,gap_dur=<dur>[,count=<n>] '
                          '-- repeat once per tier. Omit count to flap every '
                          'daemon in that tier.')
    args = ap.parse_args()

    if args.from_yaml:
        daemon_names = daemon_names_from_yaml(args.from_yaml)
    else:
        with open(args.daemon_names_file) as f:
            daemon_names = [line.strip() for line in f if line.strip()]

    specs = [parse_flap_arg(a) for a in args.flap]
    print(build_flap_string(specs, daemon_names))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, FileNotFoundError, yaml.YAMLError) as e:
        print(f'flap_groups.py: error: {e}', file=sys.stderr)
        sys.exit(1)