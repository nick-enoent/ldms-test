import os
import re
import sys
import pwd
import glob
import time
import json
import errno
import socket
import docker
import ipaddress as ip
import subprocess

import TADA

import pdb

from functools import wraps
from io import StringIO, BytesIO
from distutils.version import LooseVersion
from distutils.spawn import find_executable

from functools import reduce

# `D` Debug object to store values for debugging
class Debug(object): pass
D = Debug()

#############
#  Helpers  #
#############

class cached_property(object):
    """`@cached_property` decorator to make a cached property (getter-only)

    NOTE: The property's value is stored in `self.__cache__` dictionary.
    """
    def __init__(self, func):
        self.func = func
        self.name = func.__name__

    def __get__(self, obj, _type):
        cache = getattr(obj, "__cache__", dict())
        if not cache: # newly created cache
            obj.__cache__ = cache
        try:
            return cache[self.name]
        except KeyError:
            cache[self.name] = val = self.func(obj)
            return val

_META_BEGIN = r'(?P<meta_begin>Schema\s+Instance\s+Flags.*\s+Info)'
_META_DASHES = r'(?:^(?P<meta_dashes>[ -]+)$)'
_META_SUMMARY = '(?:'+ \
                r'Total Sets: (?P<meta_total_sets>\d+), ' + \
                r'Meta Data \(kB\):? (?P<meta_sz>\d+(?:\.\d+)?), ' + \
                r'Data \(kB\):? (?P<data_sz>\d+(?:\.\d+)?), ' + \
                r'Memory \(kB\):? (?P<mem_sz>\d+(?:\.\d+)?)' + \
                ')'
_META_DATA = r'(?:' + \
             r'(?P<meta_schema>\S+)\s+' + \
             r'(?P<meta_inst>\S+)\s+' + \
             r'(?P<meta_flags>\D+)\s+' + \
             r'(?P<meta_msize>\d+)\s+' + \
             r'(?P<meta_dsize>\d+)\s+' + \
             r'(?P<meta_uid>\d+)\s+' + \
             r'(?P<meta_gid>\d+)\s+' + \
             r'(?P<meta_perm>-(?:[r-][w-][x-]){3})\s+' + \
             r'(?P<meta_update>\d+\.\d+)\s+' + \
             r'(?P<meta_duration>\d+\.\d+)' + \
             r'(?:\s+(?P<meta_info>.*))?' + \
             r')'
_META_END = r'(?:^(?P<meta_end>[=]+)$)'
_LS_L_HDR = r'(?:(?P<set_name>[^:]+): .* last update: (?P<ts>.*))'
_LS_L_DATA = r'(?:(?P<F>.) (?P<type>\S+)\s+(?P<metric_name>\S+)\s+' \
             r'(?P<metric_value>.*))'
_LS_RE = re.compile(
            _META_BEGIN + "|" +
            _META_DASHES + "|" +
            _META_DATA + "|" +
            _META_SUMMARY + "|" +
            _META_END + "|" +
            _LS_L_HDR + "|" +
            _LS_L_DATA
         )
def int0(s):
    return int(s, base=0)
_TYPE_FN = {
    "char": lambda x: str(x).strip("'"),
    "char[]": lambda x: str(x).strip('"'),

    "u8": int0,
    "s8": int0,
    "u16": int0,
    "s16": int0,
    "u32": int0,
    "s32": int0,
    "u64": int0,
    "s64": int0,
    "f32": float,
    "d64": float,

    "u8[]": lambda x: list(map(int0, x.split(','))),
    "s8[]": lambda x: list(map(int0, x.split(','))),
    "u16[]": lambda x: list(map(int0, x.split(','))),
    "s16[]": lambda x: list(map(int0, x.split(','))),
    "u32[]": lambda x: list(map(int0, x.split(','))),
    "s32[]": lambda x: list(map(int0, x.split(','))),
    "u64[]": lambda x: list(map(int0, x.split(','))),
    "s64[]": lambda x: list(map(int0, x.split(','))),
    "f32[]": lambda x: list(map(float, x.split(','))),
    "d64[]": lambda x: list(map(float, x.split(','))),
}

def parse_ldms_ls(txt):
    """Parse output of `ldms_ls -l [-v]` into { SET_NAME : SET_DICT } dict

    Each SET_DICT is {
        "name" : SET_NAME,
        "ts" : UPDATE_TIMESTAMP_STR,
        "meta" : {
            "schema"    :  SCHEMA_NAME,
            "instance"  :  INSTANCE_NAME,
            "flags"     :  FLAGS,
            "meta_sz"   :  META_SZ,
            "data_sz"   :  DATA_SZ,
            "uid"       :  UID,
            "gid"       :  GID,
            "perm"      :  PERM,
            "update"    :  UPDATE_TIME,
            "duration"  :  UPDATE_DURATION,
            "info"      :  APP_INFO,
        },
        "data" : {
            METRIC_NAME : METRIC_VALUE,
            ...
        },
        "data_type" : {
            METRIC_NAME : METRIC_TYPE,
        },
    }
    """
    ret = dict()
    lines = txt.splitlines()
    itr = iter(lines)
    meta_section = False
    for l in itr:
        l = l.strip()
        if not l: # empty line, end of set
            lset = None
            data = None
            meta = None
            continue
        m = _LS_RE.match(l)
        if not m:
            raise RuntimeError("Bad line format: {}".format(l))
        m = m.groupdict()
        if m["meta_begin"]: # start meta section
            if meta_section:
                raise RuntimeError("Unexpected meta info: {}".format(l))
            meta_section = True
            continue
        elif m["meta_schema"]: # meta data
            if not meta_section:
                raise RuntimeError("Unexpected meta info: {}".format(l))
            meta = dict( schema = m["meta_schema"],
                         instance = m["meta_inst"],
                         flags = m["meta_flags"],
                         meta_sz = m["meta_msize"],
                         data_sz = m["meta_dsize"],
                         uid = m["meta_uid"],
                         gid = m["meta_gid"],
                         perm = m["meta_perm"],
                         update = m["meta_update"],
                         duration = m["meta_duration"],
                         info = m["meta_info"],
                    )
            _set = ret.setdefault(m["meta_inst"], dict())
            _set["meta"] = meta
            _set["name"] = m["meta_inst"]
        elif m["meta_total_sets"]: # the summary line
            if not meta_section:
                raise RuntimeError("Unexpected meta info: {}".format(l))
            # else do nothing
            continue
        elif m["meta_dashes"]: # dashes
            if not meta_section:
                raise RuntimeError("Unexpected meta info: {}".format(l))
            continue
        elif m["meta_end"]: # end meta section
            if not meta_section:
                raise RuntimeError("Unexpected meta info: {}".format(l))
            meta_section = False
            continue
        elif m["set_name"]: # new set
            if meta_section:
                raise RuntimeError("Unexpected data info: {}".format(l))
            data = dict() # placeholder for metric data
            data_type = dict() # placeholder for metric data type
            lset = ret.setdefault(m["set_name"], dict())
            lset["name"] = m["set_name"]
            lset["ts"] = m["ts"]
            lset["data"] = data
            lset["data_type"] = data_type
        elif m["metric_name"]: # data
            if meta_section:
                raise RuntimeError("Unexpected data info: {}".format(l))
            if m["type"] == "char[]":
                _val = m["metric_value"]
            else:
                _val = m["metric_value"].split(' ', 1)[0] # remove units
            mname = m["metric_name"]
            mtype = m["type"]
            data[mname] = _TYPE_FN[mtype](_val)
            data_type[mname] = mtype
        else:
            raise RuntimeError("Unable to process line: {}".format(l))
    return ret

def create_suite_from_C_test_results(txt, tada_addr):
    import json

    r = json.loads(txt)
    cnt = len(r)

    test = TADA.Test(test_suite = r[0]["test-suite"],
                     test_type = r[0]["test-type"],
                     test_name = r[0]["test-name"],
                     test_desc = r[0]["test-desc"],
                     test_user = r[0]["test-user"],
                     commit_id = r[0]["commit-id"],
                     tada_addr = tada_addr)

    for msg in r:
        if msg["msg-type"] == "assert-status":
            test.add_assertion(msg["assert-no"], msg["assert-desc"])

    test.start()

    for msg in r:
        if msg["msg-type"] == "assert-status":
            result = True if (msg["test-status"] == "passed") else False
            test.assert_test(msg["assert-no"], result, msg["assert-cond"])

    test.finish()

def env_dict(env):
    """Make env dict(NAME:VALUE) from list(NAME=VALUE) or dict(NAME:VALUE)

    Docker API understands two forms of `env` parameters: list(NAME=VALUE) and
    dict(NAME:VALUE). This function is to convert them into dict form to make us
    work a little easier.
    """
    if type(env) == dict:
        return dict(env) # return a copy
    if type(env) != list:
        raise TypeError("`env` is not a list nor a dict")
    return dict( e.split("=", 1) for e in env )

def get_docker_clients():
    """Get all docker clients to dockerds in the swarm"""
    dc = docker.from_env()
    nodes = dc.nodes.list()
    addrs = [ n.attrs["Description"]["Hostname"] for n in nodes ]
    addrs.sort()
    return [ docker.DockerClient(base_url = "tcp://{}:2375".format(a)) \
                    for a in addrs ]

def jprint(obj):
    """Pretty print JSON object"""
    print(json.dumps(obj, indent=2))

def get_ovis_commit_id(prefix):
    """Get commit_id of the ovis installation"""
    try:
        path = "{}/bin/ldms-pedigree".format(prefix)
        f = open(path)
        for l in f.readlines():
            if l.startswith("echo commit-id: "):
                e, c, commit_id = l.split()
                return commit_id
    except:
        pass
    return "NONE"

def guess_ovis_prefix():
    """Guess ovis prefix from the environment"""
    sbin_ldmsd = find_executable("ldmsd")
    if sbin_ldmsd:
        prefix, a, b = sbin_ldmsd.rsplit('/', 2)
    else:
        prefix = "/opt/ovis"
    return prefix

ADDR_RE = re.compile(r'^(?P<addr>[^:]+)(?:[:](?P<port>\d+))?$')
def tada_addr(s):
    m = ADDR_RE.match(s)
    if not m:
        raise ValueError("Bad address format")
    m = m.groupdict()
    if m["port"]:
        return s # already has port specified
    else:
        return s + ":9862" # default port

def get_cluster_name(parsed_args):
    """Derive `clustername` from the parsed CLI arguments"""
    if parsed_args.clustername:
        return parsed_args.clustername
    uname = parsed_args.user
    test = os.path.basename(sys.argv[0])
    if test.endswith('.py'):
        test = test[:-3]
    commit_id = get_ovis_commit_id(parsed_args.prefix)
    parsed_args.clustername = "{}-{}-{:.7}".format(uname, test, commit_id)
    return parsed_args.clustername

def add_common_args(parser):
    """Add common arguments for test scripts"""
    _USER = pwd.getpwuid(os.geteuid())[0]
    parser.add_argument("--clustername", type = str,
            help = "The name of the cluster. The default is "
            "{USER}-{TEST_NAME}-{COMMIT_ID}.")
    parser.add_argument("--user", default = _USER,
            help = "Specify the user who run the test.")
    parser.add_argument("--prefix", type = str,
            default = guess_ovis_prefix(),
            help = "The OVIS installation path on the host. This will be mounted to /opt/ovis in containers.")
    parser.add_argument("--src", type = str,
            help = "The path to OVIS source tree (for gdb). " \
            "If not specified, src tree won't be mounted.")
    parser.add_argument("--data_root", "--data-root", type = str,
            help = "The path to host db directory. The default is "
                   "'/home/{user}/db/{clustername}'" )
    parser.add_argument("--tada_addr", "--tada-addr", type=tada_addr,
            help="The test automation server host and port as host:port.",
            default="tada-host:9862")
    parser.add_argument("--debug", action="store_true",
            help="Turn on TADA.DEBUG flag.")
    parser.add_argument("--mount", action="append",
            metavar = "SRC:DST[:MODE]", default = [],
            help="Add additional mount point to the container. "
                 "SRC is the path on the host. DST is the path in the "
                 "container. MODE can be `ro` or `rw`. If MODE is not given, "
                 "the default is `rw`. Example: --mount /mnt/abc:/home:ro."
        )

def process_args(parsed_args):
    """Further process the parsed common arguments"""
    args = parsed_args
    args.clustername = get_cluster_name(args)
    if not args.data_root:
        args.data_root = os.path.expanduser("~{a.user}/db/{a.clustername}".format(a = args))
    if not os.path.exists(args.data_root):
        os.makedirs(args.data_root)
    args.commit_id = get_ovis_commit_id(args.prefix)
    if args.debug:
        TADA.DEBUG = True

DEEP_COPY_TBL = {
        dict: lambda x: { k:deep_copy(v) for k,v in x.items() },
        list: lambda x: [ deep_copy(v) for v in x ],
        tuple: lambda x: tuple( deep_copy(v) for v in x ),
        int: lambda x: x,
        float: lambda x: x,
        str: lambda x: x,
        bool: lambda x: x,
    }

def deep_copy(obj):
    t = type(obj)
    f = DEEP_COPY_TBL.get(t)
    if not f:
        raise TypeError("Unsupported type: {.__name__}".format(t))
    return f(obj)

def debug_prompt(prompt = "Press ENTER to continue or Ctrl-C to debug"):
    """Display a given prompt if DEBUG mode and interactive flag are on"""
    if sys.flags.interactive and TADA.DEBUG:
        if sys.version_info.major == 3: # python3
            return input(prompt)
        elif sys.version_info.major == 2: # python2
            return raw_input(prompt)
        else:
            raise RuntimeError("Unknown python version: {}" \
                               .format(sys.version_info.major))

class Spec(dict):
    """Spec object -- handling spec object extension and substitution

    Synopsis:
    >>> spec_def = {...} # see example below
    >>> spec = Spec(spec_def)

    A Spec is a dictionary with a reserved top-level "templates" attribute for
    defining object (dict) templates. The objects in the spec can extend
    a template using a reserved attribute "!extends".  The local attributes
    override those from the template. The "%VAR%" in the string inside the Spec
    is substituted with the value of the attribute of the nearest parent object
    (self is the nearest).

    The substitution is done after the recursive extension is done.

    For example,

    {
        "USER": "root",
        "templates": {
            "prog-base": {
                "path": "/bin/%prog%",
                "desc": "This is %prog%",
                "user": "%USER%",
            },
            "prog-sbin": {
                "!extends": "prog-base",
                "path": "/sbin/%prog%",
            },
        },
        "programs": [
            {
                "prog": "ssh",
                "!extends": "prog-base",
            },
            {
                "prog": "sshd",
                "!extends": "prog-sbin",
            },
        ],
        "jail": {
            "USER": "jail",
            "programs": [
                {
                    "prog": "ls",
                    "!extends": "prog-base",
                },
            ],
        },
    }

    will be extended and substituted as:

    {
        "USER": "root",
        "templates": {
            "prog-base": {
                "path": "/bin/%prog%",
                "desc": "This is %prog%",
                "user": "%USER%",
            },
            "prog-sbin": {
                "!extends": "prog-base",
                "path": "/sbin/%prog%",
            },
        },
        "programs": [
            {
                "prog": "ssh",
                "path": "/bin/ssh",
                "desc": "This is ssh",
                "user": "root",
            },
            {
                "prog": "sshd",
                "path": "/sbin/sshd",
                "desc": "This is sshd",
                "user": "root",
            },
        ],
        "jail": {
            "USER": "jail",
            "programs": [
                {
                    "prog": "ls",
                    "path": "/bin/ls",
                    "desc": "This is ls",
                    "user": "jail",
                },
            ],
        },
    }

    """
    MAX_DEPTH = 64
    VAR_RE = re.compile(r'%([^%]+)%')
    PRIMITIVES = set([int, float, bool, str])

    def __init__(self, spec):
        _dict = deep_copy(spec)
        self.templates = _dict.get("templates", {})
        super(Spec, self).__init__(_dict)
        self.SUBST_TBL = {
            dict: self._subst_dict,
            list: self._subst_list,
            tuple: self._subst_tuple,
            str: self._subst_str,
            int: self._subst_scalar,
            float: self._subst_scalar,
            bool: self._subst_scalar,
        }
        self.EXPAND_TBL = {
            dict: self._expand_dict,
            list: self._expand_list,
            tuple: self._expand_tuple,
            int: self._expand_scalar,
            float: self._expand_scalar,
            str: self._expand_scalar,
            bool: self._expand_scalar,
        }
        self._start_expand()
        self._start_subst()

    def _start_expand(self):
        """(private) starting point of template expansion"""
        for k,v in self.items():
            if k == "templates":
                continue # skip the templates
            self[k] = self._expand(v, 0)

    def _start_subst(self):
        """(private) starting point of %VAR% substitute"""
        self.VAR = { k:v for k,v in self.items() \
                         if type(v) in self.PRIMITIVES }
        for k,v in self.items():
            if k == "templates":
                continue
            self[k] = self._subst(v)

    def _expand(self, obj, lvl):
        """(private) Expand the "!extends" and "%VAR%" """
        if lvl > self.MAX_DEPTH:
            raise RuntimeError("Expansion exceeding maximum depth ({})" \
                               .format(self.MAX_DEPTH))
        tp = type(obj)
        fn = self.EXPAND_TBL.get(tp)
        if not fn:
            raise TypeError("Unsupported type {.__name__}".format(tp))
        return fn(obj, lvl)

    def _expand_scalar(self, obj, lvl):
        return obj

    def _expand_list(self, lst, lvl):
        return [ self._expand(x, lvl+1) for x in lst ]

    def _expand_tuple(self, tpl, lvl):
        return tuple( self._expand(x, lvl+1) for x in tpl )

    def _expand_dict(self, dct, lvl):
        lst = [dct] # list of extension
        ext = dct.get("!extends")
        while ext:
            _temp = self.templates.get(ext)
            if _temp == None:
                raise KeyError("`{}` template not found".format(ext))
            lst.append(_temp)
            ext = _temp.get("!extends")
        # new temporary dict
        tmp = dict()
        while lst:
            # update dict by extension order, base first
            d = lst.pop()
            tmp.update(d)
        tmp.pop("!extends", None) # remove the "!extends" keyword
        return { k: self._expand(v, lvl+1) for k,v in tmp.items() }

    def _subst(self, obj):
        """(private) substitute %VAR% """
        tp = type(obj)
        fn = self.SUBST_TBL.get(tp)
        if not fn:
            raise TypeError("Unsupported type {.__name__}".format(tp))
        return fn(obj)

    def _subst_scalar(self, val):
        return val

    def _subst_list(self, lst):
        return [ self._subst(x) for x in lst ]

    def _subst_tuple(self, tpl):
        return tuple( self._subst(x) for x in tpl )

    def _subst_dict(self, dct):
        _save = self.VAR
        # new VAR dict
        var = dict(self.VAR)
        var.update( { k:v for k,v in dct.items() \
                          if type(v) in self.PRIMITIVES } )
        self.VAR = var
        _ret = { k: self._subst(v) for k,v in dct.items() }
        # recover
        self.VAR = _save
        return _ret

    def _subst_str(self, val):
        return self.VAR_RE.sub(lambda m: str(self.VAR[m.group(1)]), val)


#####################################################
#                                                   #
#   Convenient wrappers for docker.models classes   #
#                                                   #
#####################################################

class Container(object):
    """Docker Container Wrapper

    This class wraps docker.models.containers.Container, providing additional
    convenient methods (such as `read_file()` and `write_file()`) and properties
    (e.g. `ip_addr`). The wrapper only exposes the following APIs:
        - attrs : dict() of Container attributes,
        - name : the name of the container,
        - client : the docker client handle for manipulating the container,
        - exec_run() : execute a program inside the container,
        - remove() : remove the container.

    The following is the additional convenient properties and methods:
        - interfaces : a list of (network_name, IP_address) of the container,
        - ip_addr : the IP address of the first interface.
        - hostname : the hostname of the container,
        - env : the environment variables of the container (from config),
        - write_file() : a utility to write data to a file in the container,
        - read_file() : a utility to read a file in the container (return str).

    """
    def __init__(self, obj):
        if not isinstance(obj, docker.models.containers.Container):
            raise TypeError("obj is not a docker Container")
        self.obj = obj
        self.attrs = obj.attrs
        self.name = obj.name
        self.client = obj.client

    def is_running(self):
        """Check if the container is running"""
        try:
            return self.obj.attrs["State"]["Status"] == "running"
        except:
            return False

    def wait_running(self, timeout=10):
        """Wait until the container become "running" or timeout

        Returns
        -------
        True  if the container becomes running before timeout
        False otherwise
        """
        t0 = time.time()
        while not self.is_running():
            t1 = time.time()
            if t1-t0 > timeout:
                return False
            self.obj.reload()
            time.sleep(1)
        return True

    def exec_run(self, *args, **kwargs):
        self.wait_running()
        (rc, out) = self.obj.exec_run(*args, **kwargs)
        if type(out) == bytes:
            return (rc, out.decode())
        return (rc, out)

    def exec_interact(self, cmd):
        """Execute `cmd` in the container with an interactive TTY

        Returns a ContainerTTY for communicating to the process spawned from
        `cmd` inside the container.
        """
        (rc, out) = self.exec_run(cmd, stdout=True, stderr=True, stdin=True,
                                  tty=True, socket=True)
        return ContainerTTY(out)

    def remove(self, **kwargs):
        self.obj.remove(**kwargs)

    @property
    def ip_addr(self):
        try:
            for name, addr in self.interfaces:
                if name == self.cluster.net.name:
                    return addr
            return None
        except:
            return None

    @property
    def interfaces(self):
        """Return a list() of (network_name, IP_address) of the container."""
        return [ (k, v['IPAddress']) for k, v in \
                 self.attrs['NetworkSettings']['Networks'].items() ]

    @property
    def hostname(self):
        """Return hostname of the container"""
        return self.attrs["Config"]["Hostname"]

    @property
    def env(self):
        """Return environment from container configuration.

        Please note that the environment in each `exec_run` may differ.
        """
        return self.attrs["Config"]["Env"]

    def write_file(self, path, content, user = None):
        """Write `content` to `path` in the container"""
        cmd = "/bin/bash -c 'cat - >{} && echo -n true'".format(path)
        rc, sock = self.exec_run(cmd, stdin=True, socket=True, user = user)
        sock = sock._sock # get the raw socket
        sock.setblocking(True)
        if type(content) == str:
            content = content.encode()
        sock.send(content)
        sock.shutdown(socket.SHUT_WR)
        D.ret = ret = sock.recv(8192)
        # skip 8-byte header
        ret = ret[8:].decode()
        sock.close()
        if ret != "true":
            raise RuntimeError(ret)

    def read_file(self, path):
        """Read file specified by `path` from the container"""
        cmd = "cat {}".format(path)
        rc, output = self.exec_run(cmd)
        if rc:
            raise RuntimeError("Error {} {}".format(rc, output))
        return output

    def chmod(self, mode, path):
        """chmod `mode` `path`"""
        cmd = "chmod {:o} {}".format(int(mode), path)
        rc, output = self.exec_run(cmd)
        if rc:
            raise RuntimeError("Error {} {}".format(rc, output))

    def chown(self, owner, path):
        """chown `owner` `path`"""
        cmd = "chown {} {}".format(owner, path)
        rc, output = self.exec_run(cmd)
        if rc:
            raise RuntimeError("Error {} {}".format(rc, output))

    def pipe(self, cmd, content):
        """Pipe `content` to `cmd` executed in the container"""
        rc, sock = self.exec_run(cmd, stdin=True, socket=True)
        sock = sock._sock
        sock.setblocking(True)
        if type(content) == str:
            content = content.encode()
        sock.send(content)
        sock.shutdown(socket.SHUT_WR)
        D.ret = ret = sock.recv(8192)
        sock.close()
        if len(ret) == 0:
            rc = 0
            output = ''
        else:
            # skip 8-byte header
            output = ret[8:].decode()
            rc = ret[0]
            if rc == 1: # OK
                rc = 0
        return rc, output

    def proc_environ(self, pid):
        """Returns environment (dict) of process `pid`"""
        _env = self.read_file("/proc/{}/environ".format(pid))
        _env = _env.split('\x00')
        _env = dict( v.split('=', 1) for v in _env if v )
        return _env

    def start(self, *args, **kwargs):
        return self.obj.start(*args, **kwargs)

    def stop(self, *args, **kwargs):
        return self.obj.stop(*args, **kwargs)


class Service(object):
    """Docker Service Wrapper

    This class wraps docker.models.services.Service and provides additional
    convenient properties and methods. The wrapper only exposes a subset of
    docker Service APIs as follows:
        - attrs : the dictionary containing docker Service information,
        - name : the name of the docker Service,
        - client : the docker Client handle.
        - remove() : remove the service

    The following is the list of additional properties and methods provided by
    this class:
        - mounts : mount points (from config),
        - env : environment variables (from config),
        - net : docker Network associated with this service,
        - containers : a list of wrapped Container objects (cached property of
          get_containers()),
        - tasks_running() : check if all tasks (containers) are running,
        - wait_tasks_running() : block-waiting until all tasks become running
          or timeout,
        - get_containers() : return a list of wrapped Container objects in the
          service,
        - build_etc_hosts() : returns `str` build for `/etc/hosts` for the
          containers of this service (as a virtual cluster),
        - update_etc_hosts() : like build_etc_hosts(), but also overwrite
          `/etc/hosts` in all containers of this service.
    """
    def __init__(self, obj):
        if not isinstance(obj, docker.models.services.Service):
            raise TypeError("obj is not a docker Service")
        self.obj = obj
        self.attrs = obj.attrs # expose attrs
        self.name = obj.name
        self.client = obj.client

    def remove(self):
        self.obj.remove()

    def tasks_running(self):
        """Returns `True` if all tasks are in "running" state"""
        try:
            tasks = self.obj.tasks()
            nrep = self.obj.attrs["Spec"]["Mode"]["Replicated"]["Replicas"]
            if len(tasks) != nrep:
                return False
            for t in tasks:
                if t['Status']['State'] != 'running':
                    return False
        except:
            return False
        return True

    def wait_tasks_running(self, timeout = 10):
        """Return `True` if all tasks become "running" before timeout (sec)"""
        t0 = time.time()
        while not self.tasks_running():
            t1 = time.time()
            if t1-t0 > timeout:
                return False
            time.sleep(1)
        return True

    def get_containers(self, timeout = 10):
        """Return a list of docker Container within the service"""
        if not self.wait_tasks_running(timeout = timeout):
            raise RuntimeError("Some tasks (containers) are not running.")
        cont_list = list()
        d = docker.from_env()
        for t in self.obj.tasks():
            D.t = t
            nid = t['NodeID']
            node = d.nodes.get(nid)
            addr = node.attrs['Description']['Hostname'] + ":2375"
            # client to remote dockerd
            ctl = docker.from_env(environment={'DOCKER_HOST': addr})
            cont_id = t['Status']['ContainerStatus']['ContainerID']
            D.cont = cont = ctl.containers.get(cont_id)
            cont_list.append(Container(cont))
        cont_list.sort(lambda a,b: cmp(LooseVersion(a.name),LooseVersion(b.name)))
        return cont_list

    def build_etc_hosts(self, node_aliases = {}):
        """Returns the generated `/etc/hosts` content"""
        sio = StringIO()
        sio.write("127.0.0.1 localhost\n")
        for cont in self.containers:
            name = cont.attrs["Config"]["Hostname"]
            networks = cont.attrs["NetworkSettings"]["Networks"]
            for net_name, net in networks.items():
                addr = net["IPAddress"]
                sio.write("{} {}".format(addr, name))
                alist = node_aliases.get(name, [])
                if type(alist) == str:
                    alist = [ alist ]
                for a in alist:
                    sio.write(" {}".format(a))
                sio.write("\n")
        return sio.getvalue()

    def update_etc_hosts(self, node_aliases = {}):
        """Update entries in /etc/hosts"""
        etc_hosts = self.build_etc_hosts(node_aliases = node_aliases)
        conts = self.get_containers()
        for cont in conts:
            cont.write_file("/etc/hosts", etc_hosts)

    @property
    def mounts(self):
        cont_spec = self.attrs['Spec']['TaskTemplate']['ContainerSpec']
        mounts = []
        for m in cont_spec.get('Mounts', []):
            mode = "ro" if "ReadOnly" in m else "rw"
            mounts.append("{}:{}:{}".format(m['Source'], m['Target'], mode))
        return mounts

    @property
    def env(self):
        """Cluster-wide environments"""
        cont_spec = self.attrs['Spec']['TaskTemplate']['ContainerSpec']
        return cont_spec.get('Env', [])

    @cached_property
    def containers(self):
        return self.get_containers(timeout = 60)

    @property
    def net(self):
        return [ self.client.networks.get(n["Target"]) \
                    for n in self.attrs["Spec"]["TaskTemplate"]["Networks"] ]

    @property
    def labels(self):
        return self.attrs["Spec"]["Labels"]


class Network(object):
    """Docker Network Wrapper"""

    def __init__(self, obj):
        if type(obj) != docker.models.networks.Network:
            raise TypeError("obj is not a docker Network object")
        self.obj = obj
        self.clients = get_docker_clients()

    @classmethod
    def create(cls, name, driver='overlay', scope='swarm', attachable=True,
                    labels = None, subnet = None):
        """A utility to create and wrap the docker network"""
        client = docker.from_env()
        try:
            if subnet:
                # NOTE: `iprange` is for docker automatic IP assignment.
                #       Since LMDS_Test manually assign IP addresses, we will
                #       limit docker iprange to be a small number.
                #       `gateway` is the max host IP address.
                ip_net = ip.ip_network(subnet)
                bc = int(ip_net.broadcast_address)
                gateway = str(ip.ip_address(bc - 1))
                iprange = str(ip.ip_address(bc & ~3)) + "/30"
                ipam_pool = docker.types.IPAMPool(subnet=subnet,
                                        iprange=iprange, gateway=gateway)
                ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
                obj = client.networks.create(name=name, driver=driver,
                                     ipam=ipam_config,
                                     scope=scope, attachable=attachable,
                                     labels = labels)
            else:
                obj = client.networks.create(name=name, driver=driver,
                                     scope=scope, attachable=attachable,
                                     labels = labels)
        except docker.errors.APIError as e:
            if e.status_code != 409: # other error, just raise it
                raise
            msg = e.explanation + ". This could be an artifact from previous " \
                  "run. To remove the network, all docker objects using net " \
                  "network must be remvoed first (e.g. service, container). " \
                  "Then, remove the network with `docker network rm {}`." \
                  .format(name)
            raise RuntimeError(msg)
        return Network(obj)

    @classmethod
    def get(cls, name, create = False, **kwargs):
        """Find (or optionally create) and wrap docker network"""
        client = docker.from_env()
        try:
            obj = client.networks.get(name)
        except docker.errors.NotFound:
            if not create:
                raise
            obj = Network.create(name, **kwargs)
        return Network(obj)

    @property
    def name(self):
        return self.obj.name

    @property
    def short_id(self):
        return self.obj.short_id

    def rm(self):
        self.obj.remove()

    def remove(self):
        self.obj.remove()

    @property
    def containers(self):
        """Containers in the network"""
        conts = []
        for c in self.clients:
            try:
                obj = c.networks.get(self.name)
            except docker.errors.NotFound:
                continue # skip clients not participating in our network
            D.obj = obj
            _conts = obj.attrs["Containers"]
            _conts = _conts if _conts else {}
            for cont_id in _conts:
                try:
                    cont = c.containers.get(cont_id)
                    conts.append(Container(cont))
                except docker.errors.NotFound:
                    continue # ignore the host-side endpoint appearing as
                             # container.
        return conts

    @property
    def labels(self):
        """Get labels"""
        return self.obj.attrs["Labels"]

    def connect(self, container, *args, **kwargs):
        return self.obj.connect(container, *args, **kwargs)


################################################################################


class DockerClusterContainer(Container):
    """A Container wrapper for containers in DockerCluster"""
    def __init__(self, obj, cluster):
        self.cluster = cluster
        super(DockerClusterContainer, self).__init__(obj)

    @property
    def aliases(self):
        """The list of aliases of the container hostname"""
        return self.cluster.node_aliases.get(self.hostname, [])

class DockerCluster(object):
    """Docker Cluster

    A utility to create a virtual cluster with Docker Swarm Network and Docker
    Containers. Instead of relying on Docker Service to orchestrate docker
    containers, which at the time this is written cannot add needed capabilities
    to the containers, this class manages the created containers itself.  The
    virtual cluster has exactly one overlay network. The hostnames of the nodes
    inside the virtual cluster is "node-{slot}", where the `{slot}` is the task
    slot number of the container.  `/etc/hosts` of each container is also
    modified so that programs inside each container can use the hostname to
    commnunicate to each other. Hostname aliases can also be set at `create()`.

    DockerCuster.create() creates the virtual cluster (as well as docker
    Service). DockerCluster.get() obtains the existing virtual cluster,
    and can optionally create a new virtual cluster if `create = True`. See
    `create()` and `get()` class methods for more details.
    """
    def __init__(self, obj):
        """Do not direcly call this, use .create() or .get() instead"""
        # obj must be a docker network with DockerCluster label
        if type(obj) != docker.models.networks.Network:
            raise TypeError("`obj` must be a docker Network")
        lbl = obj.attrs.get("Labels")
        if "DockerCluster" not in lbl:
            msg = "The network is not created by DockerCluster. " \
                  "Please remove or disconnect docker containers " \
                  "using the network first, then remove the network " \
                  "by `docker network rm {}`. " \
                  .format(obj.name)
            raise TypeError(msg)
        self.obj = obj
        self.net = Network(obj)
        self.cont_dict = None

    @classmethod
    def create(cls, name, image = "centos:7", nodes = 8,
                    mounts = [], env = [], labels = {},
                    node_aliases = {},
                    cap_add = [],
                    cap_drop = [],
                    subnet = None,
                    host_binds = {}):
        """Create virtual cluster with docker network and service

        If the docker network existed, this will failed. The hostname of each
        container in the virtual cluster is formatted as "node-{slot}", where
        '{slot}' is the docker task slot number for the container. Applications
        can set node aliases with `node_aliases` parameter.

        Example
        -------
        >>> cluster = DockerCluster.create(
                            name = "vc", nodes = 16,
                            mounts = [ "/home/bob/ovis:/opt/ovis:ro" ],
                            env = { "CLUSTERNAME" : "vc" },
                            node_aliases = { "node-1" : [ "head" ] },
                            cap_add = [ "SYS_PTRACE" ]
                        )

        Parameters
        ----------
        name : str
            The name of the cluster (also the name of the network).
        image : str
            The name of the image to use.
        nodes : int
            The number of nodes in the virtual cluster.
        mounts : list(str)
            A list of `str` of mount points with format "SRC:DEST:MODE",
            in which "SRC" being the source path (on the docker host),
            "DEST" being the mount destination path (in the container),
            and "MODE" being "ro" or "rw" (read-only or read-write).
        env : list(str) or dict(str:str)
            A list of "NAME=VALUE", or a dictionary of { NAME: VALUE } of
            environment variables.
        labels : dict(str:str)
            A dictionary of { LABEL : VALUE } for user-defined labels for the
            docker service.
        node_aliases : dict(str:list(str))
            A dictionary of { NAME : list( str ) } containing a list of aliases
            of the nodes.
        cap_add : list(str)
            A list of capabilities (e.g. 'SYS_PTRACE') to add to containers
            created by the virtual cluster.
        cap_drop : list(str)
            A list of capabilities to drop.

        Returns
        -------
        DockerCluster
            The virtual cluster handle.
        """
        if type(nodes) == int:
            nodes = [ "node-{}".format(i) for i in range(0, nodes) ]
        lbl = dict(labels)
        cfg = dict(name = name,
                   image = image,
                   env = env,
                   nodes = nodes,
                   mounts = mounts,
                   cap_add = cap_add,
                   cap_drop = cap_drop,
               )
        lbl.update({
                "node_aliases": json.dumps(node_aliases),
                "DockerCluster" : json.dumps(cfg),
              })
        clients = get_docker_clients()

        # allocation table by client: [current_load, alloc, client]
        tbl = [ [ cl.info()["ContainersRunning"], 0, cl ] for cl in clients ]
        # and start calculating allocation for each client
        # sort clients by load
        tbl.sort( key = lambda x: x[0] )
        max_load = tbl[-1][0] # last entry
        _n = len(nodes) # number of containers needed
        cn = len(tbl)
        # make the load equal by filling the diff from max_load first
        for ent in tbl:
            _a = max_load - ent[0]
            if not _a:
                break # reaching max_load, no need to continue
            _a = _a if _a < _n else _n
            ent[1] = _a
            _n -= _a
            if not _n:
                break # all containers allocated, no need to continue
        # evenly distribute remaining load after equalization
        _a = _n // cn
        for ent in tbl:
            ent[1] += _a
        # the remainders
        _n %= cn
        for i in range(0, _n):
            tbl[i][1] += 1

        # making parameters for containers
        _slot = 1
        cont_build = [] # store (client, cont_param)
        lbl_cont_build = [] # store (client_name, cont_param) for reference
        volumes = { src: {"bind": dst, "mode": mo } \
                    for src, dst, mo in map(lambda x:x.split(':'), mounts)
                }
        idx = 0
        for load, n, cl in tbl:
            # allocate `n` container using `client`
            cl_info = cl.info()
            cl_name = cl_info["Name"]
            for i in range(0, n):
                node = nodes[idx]
                idx += 1
                hostname = node
                cont_name = "{}-{}".format(name, node)
                cont_param = dict(
                        image = image,
                        name = cont_name,
                        command = "/bin/bash",
                        tty = True,
                        detach = True,
                        environment = env,
                        volumes = volumes,
                        cap_add = cap_add,
                        cap_drop = cap_drop,
                        #network = name,
                        hostname = hostname,
                    )
                binds = host_binds.get(hostname)
                if binds:
                    cont_param["ports"] = binds
                lbl_cont_build.append( (cl_name, cont_param) )
                cont_build.append( (cl, cont_param) )
        # memorize cont_build as a part of label
        dc = docker.from_env()
        lbl["cont_build"] = json.dumps(lbl_cont_build)
        net = Network.create(name = name, driver = "overlay",
                             attachable = True, scope = "swarm",
                             labels = lbl, subnet = subnet)
        if subnet:
            ip_net = ip.ip_network(subnet)
            ip_itr = ip_net.hosts()
        # then, create the actual containers
        for cl, params in cont_build:
            cont = cl.containers.create(**params)
            if subnet:
                ip_addr = next(ip_itr)
                net.connect(cont, ipv4_address=str(ip_addr))
            else:
                net.connect(cont)
            cont.start()
        cluster = DockerCluster(net.obj)
        cluster.update_etc_hosts(node_aliases = node_aliases)
        return cluster

    @classmethod
    def get(cls, name, create = False, **kwargs):
        """Finds (or optionally creates) and returns the DockerCluster

        This function finds the DockerCluster by `name`. If the service
        is found, everything else is ignored. If the service not found and
        `create` is `True`, DockerCluster.create() is called with
        given `kwargs`. Otherwise, `docker.errors.NotFound` is raised.

        Parameters
        ----------
        name : str
            The name of the virtual cluster.
        create : bool
            If `True`, the function creates the service if it is not found.
            Otherwise, no new service is created (default: False).
        **kwargs
            Parameters for DockerCluster.create()
        """
        try:
            dc = docker.from_env()
            net = dc.networks.get(name)
            return cls(net)
        except docker.errors.NotFound:
            if not create:
                raise
            return cls.create(name = name, **kwargs)

    def is_running(self):
        """Check if the service (all ) is running"""
        for cont in self.containers:
            if not cont.is_running():
                return False
        return True

    def wait_running(self, timeout=10):
        """Wait for all containers to run"""
        t0 = time.time()
        while not self.is_running():
            t1 = time.time()
            if t1-t0 > timeout:
                return False
            time.sleep(1)
        return True

    @cached_property
    def containers(self):
        """A list of containers wrapped by DockerClusterContainer"""
        return self.get_containers()

    def get_containers(self, timeout = 10):
        """Return a list of docker Containers of the virtual cluster"""
        our_conts = []
        clients = get_docker_clients()
        for cl in clients:
            conts = cl.containers.list(all=True)
            for cont in conts:
                if cont.attrs['NetworkSettings']['Networks'].get(self.net.name):
                    our_conts.append(cont)

        cont_list = [ DockerClusterContainer(c, self) for c in our_conts ]
        cont_list.sort(key = lambda x: LooseVersion(x.name))
        return cont_list

    def get_container(self, name):
        """Get container by name"""
        if not self.cont_dict:
            cont_list = self.containers
            cont_dict = dict()
            for cont in cont_list:
                k = cont.attrs['Config']['Hostname']
                cont_dict[k] = cont
            for k, v in self.node_aliases.items():
                cont = cont_dict[k]
                if type(v) == str:
                    v = [ v ]
                for n in v:
                    cont_dict[n] = cont
            self.cont_dict = cont_dict
        return self.cont_dict.get(name)

    @cached_property
    def node_aliases(self):
        """dict(hostname:list) - node aliases by hostname"""
        txt = self.net.obj.attrs["Labels"]["node_aliases"]
        return json.loads(txt)

    def remove(self):
        """Remove the docker service and its network"""
        for cont in self.containers:
            try:
                cont.remove(force = True)
            except:
                pass
        self.net.remove()

    @property
    def labels(self):
        """Labels"""
        return self.net.obj.attrs["Labels"]

    def build_etc_hosts(self, node_aliases = {}):
        """Returns the generated `/etc/hosts` content"""
        if not node_aliases:
            node_aliases = self.node_aliases
        sio = StringIO()
        sio.write("127.0.0.1 localhost\n")
        for cont in self.containers:
            name = cont.hostname
            ip_addr = cont.ip_addr
            sio.write("{0.ip_addr} {0.hostname}".format(cont))
            networks = cont.attrs["NetworkSettings"]["Networks"]
            alist = node_aliases.get(name, [])
            if type(alist) == str:
                alist = [ alist ]
            for a in alist:
                sio.write(" {}".format(a))
            sio.write("\n")
        return sio.getvalue()

    def update_etc_hosts(self, node_aliases = {}):
        """Update entries in /etc/hosts"""
        etc_hosts = self.build_etc_hosts(node_aliases = node_aliases)
        conts = self.get_containers()
        for cont in conts:
            cont.write_file("/etc/hosts", etc_hosts)


class LDMSDContainer(DockerClusterContainer):
    """Container wrapper for a container being a part of LDMSDCluster

    LDMSDContainer extends DockerClusterContainer -- adding ldmsd-specific
    routines and properties.

    Application does not normally direcly create LDMSDContainer. LDMSDContainer
    should be obtained by calling `get_container()` or accessing `containers` of
    LDMSDCluster.
    """
    def __init__(self, obj, svc):
        if not issubclass(type(svc), LDMSDCluster):
            raise TypeError("`svc` must be a subclass of LDMSDCluster")
        self.svc = svc
        super(LDMSDContainer, self).__init__(obj, svc)
        self.DAEMON_TBL = {
            "etcd": self.start_etcd,
            "ldmsd": self.start_ldmsd,
            "sshd": self.start_sshd,
            "munged": self.start_munged,
            "slurmd": self.start_slurmd,
            "slurmctld": self.start_slurmctld,
        }
        self.munged = dict()

    def pgrep(self, *args):
        """Return (rc, output) of `pgrep *args`"""
        return self.exec_run("pgrep " + " ".join(args))

    def pgrepc(self, prog):
        """Reurn the number from `pgrep -c {prog}`"""
        rc, out = self.pgrep("-c", prog)
        return int(out)

    def check_etcd(self):
        """Check if etcd is running"""
        rc, out = self.exec_run("pgrep -c etcd")
        return rc == 0

    def start_etcd(self, spec_overrid = {}, **kwargs):
        """Start etcd in the container"""
        if self.check_etcd():
            return
        out = self.exec_interact("etcd")
        if not out:
            raise RuntimeError("sshd failed, rc: {}, output: {}" \
                              .format(rc, out))

    def check_ldmsd(self):
        """Check if ldmsd is running"""
        rc, out = self.exec_run("pgrep -c ldmsd")
        return rc == 0

    def start_ldmsd(self, spec_override = {}, **kwargs):
        """Start ldmsd in the container"""
        if self.check_ldmsd():
            return # already running
        spec = deep_copy(self.ldmsd_spec)
        spec.update(spec_override)
        if not spec:
            return # no ldmsd spec for this node and no spec given
        cfg = self.get_ldmsd_config(spec)
        self.write_file(spec["config_file"], cfg)
        cmd = self.get_ldmsd_cmd(spec)
        self.exec_run(cmd, environment = env_dict(spec["env"]))

    def kill_ldmsd(self):
        """Kill ldmsd in the container"""
        self.exec_run("pkill ldmsd")

    @cached_property
    def spec(self):
        """Get container spec"""
        for node in self.svc.spec["nodes"]:
            if self.hostname == node["hostname"]:
                return node
        return None

    @cached_property
    def ldmsd_spec(self):
        """Get the spec for this ldmsd (from the associated service)"""
        ldmsd = next( (d for d in self.spec.get("daemons", []) \
                          if d["type"] == "ldmsd"), None )
        if not ldmsd:
            return {} # empty dict
        dspec = deep_copy(ldmsd)

        # apply cluster-level env
        w = env_dict(dspec.get("env", []))
        v = env_dict(self.svc.spec.get("env", []))
        v.update(w) # dspec precede svc_spec
        dspec["env"] = v
        dspec.setdefault("log_file", "/var/log/ldmsd.log")
        dspec.setdefault("log_level", "INFO")
        dspec.setdefault("listen_auth", "none")
        dspec.setdefault("config_file", "/etc/ldmsd.conf")
        return dspec

    @cached_property
    def ldmsd_config(self):
        """Get ldmsd config `str` of this container"""
        return self.get_ldmsd_config(self.ldmsd_spec)

    def get_ldmsd_config(self, spec):
        """Generate ldmsd config `str` from given spec"""
        sio = StringIO()
        # process `auth`
        for auth in spec.get("auth", []):
            _a = auth.copy() # shallow copy
            cfgcmd = "auth_add name={}".format(_a.pop("name")) \
                     +"".join([" {}={}".format(k, v) for k,v in _a.items()])\
                     +"\n"
            sio.write(cfgcmd)
        # process `listen`
        for listen in spec.get("listen", []):
            _l = listen.copy() # shallow copy
            cfgcmd = "listen " \
                     +"".join([" {}={}".format(k, v) for k,v in _l.items()])\
                     +"\n"
            sio.write(cfgcmd)
        # process `samplers`
        for samp in spec.get("samplers", []):
            plugin = samp["plugin"]
            interval = samp.get("interval", 2000000)
            if interval != "":
                interval = "interval={}".format(interval)
            offset = samp.get("offset", "")
            if offset != "":
                offset = "offset={}".format(offset)
            if self.ldmsd_version >= (4,100,0):
                samp_temp = \
                    "load name={plugin}\n" \
                    "config name={plugin} {config}\n"
                if samp.get("start"):
                    samp_temp += \
                        "smplr_add name={plugin}_smplr instance={plugin} " \
                        "          {interval} {offset}\n" \
                        "smplr_start name={plugin}_smplr\n"
            else:
                samp_temp = \
                    "load name={plugin}\n" \
                    "config name={plugin} {config}\n"
                if samp.get("start"):
                    samp_temp += "start name={plugin} {interval} {offset}\n"
            samp_cfg = samp_temp.format(
                plugin = plugin, config = " ".join(samp["config"]),
                interval = interval, offset = offset
            )

            # replaces all %VAR%
            samp_cfg = re.sub(r'%(\w+)%', lambda m: samp[m.group(1)], samp_cfg)
            sio.write(samp_cfg)
        # process `prdcrs`
        for prdcr in spec.get("prdcrs", []):
            prdcr = deep_copy(prdcr)
            prdcr_add = "prdcr_add name={}".format(prdcr.pop("name"))
            for k, v in prdcr.items():
                prdcr_add += " {}={}".format(k, v)
            sio.write(prdcr_add)
            sio.write("\n")
        # process `config`
        cfg = spec.get("config")
        if cfg:
            for x in cfg:
                sio.write(x)
                sio.write("\n")
        return sio.getvalue()

    @cached_property
    def ldmsd_cmd(self):
        """Command to run ldmsd"""
        return self.get_ldmsd_cmd(self.ldmsd_spec)

    def get_ldmsd_cmd(self, spec):
        """Get ldmsd command line according to spec"""
        if "listen_xprt" in spec and "listen_port" in spec:
            XPRT_OPT = "-x {listen_xprt}:{listen_port}".format(**spec)
        else:
            XPRT_OPT = ""
        if "listen_auth" in spec:
            AUTH_OPT = "-a {listen_auth}".format(**spec)
        else:
            AUTH_OPT = ""
        cmd = "ldmsd {XPRT_OPT} {AUTH_OPT}" \
              "      -c {config_file} -l {log_file} -v {log_level}" \
              .format(XPRT_OPT=XPRT_OPT, AUTH_OPT=AUTH_OPT, **spec)
        return cmd

    def ldms_ls(self, *args):
        """Executes `ldms_ls` with *args, and returns (rc, output)"""
        cmd = "ldms_ls " + (" ".join(args))
        return self.exec_run(cmd)

    def get_munged(self, dom = None):
        """Get the munged handle"""
        return self.munged.get(dom)

    def set_munged(self, dom, m):
        self.munged[dom] = m

    def start_munged(self, name = None, dom = None, key = None, **kwargs):
        """Start Munge Daemon"""
        m = self.get_munged(dom)
        if not m:
            for d in self.spec["daemons"]:
                if name == d.get("name"):
                    break
                if not name and dom == d.get("dom"):
                    break
            else: # not found
                d = dict(dom = dom, key = key)
            key = key if key else d.get("key")
            dom = dom if dom else d.get("dom")
            m = Munged(self, dom, key)
            self.set_munged(dom, m)
        m.start()

    def kill_munged(self, dom = None):
        """Kill munged"""
        m = self.get_munged(dom)
        if m:
            m.kill()

    def prep_slurm_conf(self):
        """Prepare slurm configurations"""
        self.write_file("/etc/slurm/cgroup.conf", "CgroupAutomount=yes")
        self.write_file("/etc/slurm/slurm.conf", self.svc.slurm_conf)

    def start_slurm(self):
        """Start slurmd in all sampler nodes, and slurmctld on svc node"""
        # determine our role
        daemons = self.spec.get("daemons", [])
        slurm_daemons = [ d for d in daemons \
                            if d.get("type") in set(["slurmctld", "slurmd"]) ]
        will_start = [d for d in slurm_daemons if self.pgrepc(d["type"]) == 0]
        if not will_start:
            return # no daemon to start
        self.prep_slurm_conf()
        for d in will_start:
            prog = d["type"]
            plugstack = d.get("plugstack")
            if plugstack:
                sio = StringIO()
                for p in plugstack:
                    sio.write("required" if p.get("required") else "optional")
                    sio.write(" " + p["path"])
                    for arg in p.get("args", []):
                        sio.write(" " + arg)
                    sio.write("\n")
                self.write_file("/etc/slurm/plugstack.conf", sio.getvalue())
            rc = -1
            retry = 3
            while rc != 0 and retry > 0:
                retry -= 1
                rc, out = self.exec_run(prog)
            if rc:
                raise RuntimeError("{} failed, rc: {}, output: {}" \
                               .format(prog, rc, out))

    def kill_slurm(self):
        """Kill slurmd and slurmctld"""
        self.exec_run("pkill slurmd slurmctld")

    def _start_slurmx(self, prog):
        """(private) Start slurmd or slurmctld"""
        # get default munge spec
        self.start_munged() # slurm depends on munged
        if self.pgrepc(prog) > 0:
            return # already running
        self.prep_slurm_conf()
        d = next( ( x for x in self.spec.get("daemons", []) \
                      if x.get("type") == prog) )
        plugstack = d.get("plugstack")
        if plugstack:
            sio = StringIO()
            for p in plugstack:
                sio.write("required" if p.get("required") else "optional")
                sio.write(" " + p["path"])
                for arg in p.get("args", []):
                    sio.write(" " + arg)
                sio.write("\n")
            self.write_file("/etc/slurm/plugstack.conf", sio.getvalue())
        rc = -1
        retry = 3
        while rc != 0 and retry > 0:
            retry -= 1
            rc, out = self.exec_run(prog)
        if rc:
            raise RuntimeError("{} failed, rc: {}, output: {}" \
                               .format(prog, rc, out))

    def start_slurmd(self, **kwargs):
        """Start slurmd"""
        self._start_slurmx("slurmd")

    def start_slurmctld(self, **kwargs):
        """Start slurmctld"""
        self._start_slurmx("slurmctld")

    def start_sshd(self, **kwargs):
        """Start sshd"""
        rc, out = self.pgrep("-c -x sshd")
        if rc == 0: # already running
            return
        rc, out = self.exec_run("/usr/sbin/sshd")
        if rc:
            raise RuntimeError("sshd failed, rc: {}, output: {}" \
                               .format(rc, out))

    def start_daemons(self):
        """Start all daemons according to spec"""
        for daemon in self.spec.get("daemons", []):
            tp = daemon["type"]
            fn = self.DAEMON_TBL.get(tp)
            if not fn:
                raise RuntimeError("Unsupported daemon type:{}".format(tp))
            fn(**daemon)

    def config_ldmsd(self, cmds):
        if not self.pgrepc('ldmsd'):
            raise RuntimeError("There is no running ldmsd to configure")
        spec = self.ldmsd_spec
        if type(cmds) not in (list, tuple):
            cmds = [ cmds ]
        sio = StringIO()
        D.sio = sio
        for _cmd in cmds:
            sio.write(_cmd)
            sio.write('\n')
        _listen = spec.get("listen")
        if _listen:
            _xprt = _listen[0].get("xprt", "sock")
            _port = _listen[0].get("port", 10000)
            _auth = _listen[0].get("auth", "none")
        else:
            _xprt = spec["listen_xprt"]
            _port = spec["listen_port"]
            _auth = spec["listen_auth"]
        cmd = 'bash -c \'ldmsd_controller --host {host} ' \
              '--xprt {xprt} ' \
              '--port {port} ' \
              '--auth {auth} ' \
              ' && true \' ' \
                  .format(
                      host=self.hostname,
                      xprt=_xprt,
                      port=_port,
                      auth=_auth,
                  )
        D.cmd = cmd
        rc, sock = self.exec_run(cmd, stdin=True, socket=True)
        sock = sock._sock
        sock.setblocking(True)
        sock.send(sio.getvalue().encode())
        sock.shutdown(socket.SHUT_WR)
        D.ret = ret = sock.recv(8192)
        if len(ret) == 0:
            rc = 0
            output = ''
        else:
            output = ret[8:].decode()
            rc = ret[0]
            if rc == 1: # OK
                rc = 0
        return rc, output

    @cached_property
    def ldmsd_version(self):
        rc, out = self.exec_run("ldmsd -V")
        _drop, _ver = out.splitlines()[0].split(': ')
        m = re.match(r'(\d+)\.(\d+)\.(\d+)', _ver)
        if not m:
            raise ValueError("Bad ldmsd version format: {}".format(_ver))
        ver = tuple(map(int, m.groups()))
        return ver

BaseCluster = DockerCluster

class LDMSDCluster(BaseCluster):
    """LDMSD Cluster - a virtual cluster for LDMSD

    LDMSDCluster extends DockerCluster. Similarly to
    DockerCluster, `create()` class method creates the virtual cluster,
    and `get()` class method obtains the existing virtual cluster (and
    optionally creates it if `create=True` is given), but LDMSDCluster receives
    `spec` instead of long list of keyword arguments (see `get()` and
    `create()`). The containers in LDMSDCluster is wrapped by LDMSDContainer to
    provide ldmsd-specific utilities.
    """
    @classmethod
    def create(cls, spec):
        """Create a virtual cluster for ldmsd with given `spec`

        `spec` is a dictionary describing the docker virtual cluster, nodes in
        the virtual cluster, and daemons running on them. The following list
        describes attributes in the `spec`:
          - "name" is the name of the virtual cluster.
          - "description" is a short description of the virtual cluster.
          - "templates" is a dictionary of templates to apply with
                        "!extends" special keyword attribute.
          - "cap_add" is a list of capabilities to add to the containers.
          - "cap_drop" is a list of capabilities to drop from the containers.
          - "image" is the name of the docker image to use.
          - "ovis_prefix" is the path to ovis installation in the host machine.
          - "env" is a dictionary of cluster-wide environment variables.
          - "mounts" is a list of mount points with "SRC:DST:MODE" format in
            which SRC being the source path in the HOST, DST being the
            destination path in the CONTAINER, and MODE being `rw` or `ro`.
          - "nodes" is a list of nodes, each item of which describes a node in
            the cluster.
        Templates and "%ATTR%" substitution can be used to reduce repititive
        descriptions in the spec. The "!extends" object attribute is reserved
        for instructing the spec mechanism to apply a template referred to by
        the value of "!extends" attribute. Consider the following example:
        ```
        {
            "templates": {
                "node-temp": {
                    "daemons": [
                        { "name": "sshd", "type": "sshd" },
                        {
                            "name": "sampler",
                            "type": "ldmsd",
                            "samplers": [
                                {
                                    "plugin": "meminfo",
                                    "config": [
                                        "instance=%hostname%/%plugin%",
                                    ],
                                },
                            ],
                        },
                    ],
                },
            },
            "nodes": [
                { "hostname": "node-1", "!extends": "node-temp" },
                { "hostname": "node-2", "!extends": "node-temp" },
                ...
            ],
        }
        ```
        The nodes extend "node-temp" template resulting in them having "daemons"
        defined in the template. The "%hostname%" and "%plugin%" in
        "instance=%hostname%/%plugin%" is later substituted with the nearest
        attributes by containment hierarchy. For "node-1", the string becomes
        "instance=node-1/meminfo" because the nearest "hostname" is the hostname
        attribute defined by the node object, and the nearest "plugin" attribute
        is the attribute defined in sampler plugin object. A template can also
        extends another template. The local attriute declaration overrides that
        declared by the template.

        The following paragraphs explain the node and daemon objects in the
        spec.

        The node in `spec["nodes"]` is a dictionary describing a node in the
        cluster containing the following attributes:
          - "hostname" defines the hostname of the container and is used to
            construct container name with '{spec["name"]}-{hostname}' format.
          - "env" is a dicionary of environment variables for the node which is
            merged with cluster-wide env (node-level precedes cluster-level).
          - "daemons" is a list of objects describing daemons running on the
            node.

        The daemon in `spec["nodes"][X]["daemons"]` is a dictionary describing
        supported daemons with the following common attributes:
          - "name" is the name of the daemon.
          - "type" is the type of the supported daemons, which are "etcd",
            "sshd", "munged", "slurmctld", "slurmd", and "ldmsd".

        "sshd", "munged" and "slurmd" daemons do not have extra attributes other
        than the common daemon attributes described above.

        "slurmd" daemon has the following extra attributes:
          - "plugstack" is a list of dictionary describing Slurm plugin. Each
            entry in the "plugstack" list contains the following attributes:
              - "required" can be True or False describing whether the plugin
                is required or optional. slurmd won't start if the required
                plugin failed to load.
              - "path" is the path to the plugin.
              - "args" is a list of arguments (strings) to the plugin.

        "ldmsd" daemon contains the following extra attributes:
          - "listen_port" is an integer describing the daemon listening port.
          - "listen_xprt" is the LDMS transport to use ("sock", "ugni" or
            "rdma").
          - "listen_auth" is the LDMS authentication method to use.
          - "samplers" (optional) is a list of sampler plugins (see below).
          - "prdcrs" (optional) is a list of producers (see below).
          - "config" (optional) is a list of strings for ldmsd configuration
            commands.
        The "samplers" list is processed first (if specified), then "prdcrs" (if
        specified), and "config" (if specified) is processed last.

        The sampler object in ldmsd daemon "samplers" list is described as
        follows:
          - "plugin" is the plugin name.
          - "interval" is the sample interval (in micro seconds).
          - "offset" is the sample offset (in micro seconds).
          - "start" can be True or False -- marking whether the plugin needs
            a start command (some plugins update data by events and do not
            require start command).
          - "config" is a list of strings "NAME=VALUE" for plugin
            configuration arguments.

        The producer object in the ldmsd daemon "prdcrs" list is described as
        follows:
          - "host" is the hostname of the ldmsd to connect to.
          - "port" is an integer describing the port of the target ldmsd.
          - "xprt" is the transport type of the target ldmsd.
          - "type" is currently be "active" only.
          - "interval" is the connection retry interval (in micro-seconds).

        The following is the `spec` skeleton:
            {
                "name" : "NAME_OF_THE_VIRTUAL_CLUSTER",
                "description" : "DESCRIPTION OF THE CLUSTER",
                "templates" : { # a list of templates
                    "TEMPLATE_NAME": {
                        "ATTR": VALUE,
                        ...
                    },
                    ...
                },
                "cap_add": [ "DOCKER_CAPABILITIES", ... ],
                "cap_drop": [ "DOCKER_CAPABILITIES", ... ],
                "image": "DOCKER_IMAGE_NAME",
                         # Optional image name to run each container.
                         # default: "ovis-centos-build"
                "ovis_prefix": "PATH-TO-OVIS-IN-HOST-MACHINE",
                               # Path to OVIS in the host machine. This will be
                               # mounted to `/opt/ovis` in the container.
                               # default: "/opt/ovis"
                "env" : { "FOO": "BAR" }, # additional environment variables
                                          # applied cluster-wide. The
                                          # environment variables in this list
                                          # (or dict) has the least precedence.
                "mounts": [ # additional mount points for each container, each
                            # entry is a `str` and must have the following
                            # format
                    "SRC_ON_HOST:DEST_IN_CONTAINER:MODE",
                                                 # MODE can be `ro` or `rw`
                    ...
                ],
                "nodes" : [ # a list of node spec
                    {
                        "hostname": "NODE_HOSTNAME",
                        "env": { ... }, # Environment for this node.
                                        # This is merged into cluster-wide
                                        # environment before applying to
                                        # exec_run. The variables in the
                                        # node overrides those in the
                                        # cluser-wide.
                        "daemons": [ # list of daemon spec
                            # Currently, only the following daemon types
                            # are supported: sshd, munged, slurmd, slurmctld,
                            # and ldmsd. Currently, we do not support two
                            # daemons of the same type. Each type has different
                            # options described as follows.
                            {
                                "name": "DAEMON_NAME0",
                                "type": "sshd",
                                # no extra options
                            },
                            {
                                "name": "DAEMON_NAME1",
                                "type": "munged",
                                # no extra options
                            },
                            {
                                "name": "DAEMON_NAME2",
                                "type": "slurmctld",
                                # no extra options
                            },
                            {
                                "name": "DAEMON_NAME3",
                                "type": "slurmd",
                                "plugstack" : [ # list of slurm plugins
                                    {
                                        "required" : True or False,
                                        "path" : "PATH_TO_PLUGIN",
                                        "args" : [
                                            "ARGUMENTS",
                                            ...
                                        ],
                                    },
                                    ...
                                ],
                            },
                            {
                                "name": "DAEMON_NAME4",
                                "type": "ldmsd",
                                "listen_port" : PORT,
                                "listen_xprt" : "XPRT",
                                "listen_auth" : "AUTH",
                                "samplers": [
                                    {
                                        "plugin": "PLUGIN_NAME",
                                        "interval": INTERVAL_USEC,
                                        "offset": OFFSET,
                                        "start": True or False,
                                        "config": [ # list of "NAME=VALUE"
                                                    # plugin configuration
                                            "NAME=VALUE",
                                            ...
                                        ],
                                    },
                                    ...
                                ],
                                "prdcrs": [ # each prdcr turns into prdcr_add
                                            # command.
                                    {
                                        "host" : "HOST_ADDRESS",
                                        "port" : PORT,
                                        "xprt" : "XPRT",
                                        "type" : "active",
                                        "interval" : INTERVAL_USEC,
                                    },
                                    ...
                                ],
                                "config": [
                                    # additional config commands
                                    "CONFIG_COMMANDS",
                                    ...
                                ]
                            },
                            ...
                        ],
                    },
                    ...
                ],
            }
        """
        kwargs = cls.spec_to_kwargs(Spec(spec))
        wrap = super(LDMSDCluster, cls).create(**kwargs)
        lc = LDMSDCluster(wrap.obj)
        lc.make_ovis_env()
        return lc

    @classmethod
    def get(cls, name, create = False, spec = None):
        """Obtain an existing ldmsd virtual cluster (or create if `create=True`)"""
        d = docker.from_env()
        try:
            wrap = super(LDMSDCluster, cls).get(name)
            cluster = LDMSDCluster(wrap.obj)
            if spec and Spec(spec) != cluster.spec:
                raise RuntimeError("spec mismatch")
            return cluster
        except docker.errors.NotFound:
            if not create:
                raise
            return LDMSDCluster.create(spec)

    @classmethod
    def spec_to_kwargs(cls, spec):
        """Convert `spec` to kwargs for DockerCluster.create()"""
        name = spec["name"]
        nodes = spec["nodes"]
        mounts = []
        prefix = spec.get("ovis_prefix")
        _PYTHONPATH = None
        if prefix:
            mounts += ["{}:/opt/ovis:ro".format(prefix)]
            # handling python path
            pp = glob.glob(prefix+'/lib*/python*/site-packages')
            pp = [ p.replace(prefix, '/opt/ovis', 1) for p in pp ]
            _PYTHONPATH = ':'.join(pp)
        if not _PYTHONPATH:
            _PYTHONPATH = "/opt/ovis/lib/python3.6/site-packages:" \
                          "/opt/ovis/lib64/python3.6/site-packages"
        mounts += spec.get("mounts", [])
        cap_add = spec.get("cap_add", [])
        cap_drop = spec.get("cap_drop", [])
        # assign daemons to containers using node_aliases
        node_aliases = {}
        hostnames = [ node["hostname"] for node in nodes ]
        host_binds = { node["hostname"]: node["binds"] \
                    for node in nodes if node.get("binds") }
        for node in nodes:
            a = node.get("aliases")
            if a:
                node_aliases[node["hostname"]] = a
        # starts with OVIS env
        env = {
                "PATH" : ":".join([
                        "/opt/ovis/bin",
                        "/opt/ovis/sbin",
                        "/usr/local/bin",
                        "/usr/local/sbin",
                        "/usr/bin",
                        "/usr/sbin",
                        "/bin",
                        "/sbin",
                    ]),
                "LD_LIBRARY_PATH" : "/opt/ovis/lib:/opt/ovis/lib64",
                "ZAP_LIBPATH" : "/opt/ovis/lib/ovis-ldms:/opt/ovis/lib64/ovis-ldms:/opt/ovis/lib/ovis-lib:/opt/ovis/lib64/ovis-lib",
                "LDMSD_PLUGIN_LIBPATH" : "/opt/ovis/lib/ovis-ldms:/opt/ovis/lib64/ovis-ldms",
                "PYTHONPATH" : _PYTHONPATH
            }
        env.update(env_dict(spec.get("env", {})))
        kwargs = dict(
                    name = name,
                    image = spec.get("image", "ovis-centos-build"),
                    mounts = mounts,
                    nodes = hostnames,
                    env = env,
                    labels = { "LDMSDCluster.spec": json.dumps(spec) },
                    node_aliases = node_aliases,
                    cap_add = cap_add,
                    cap_drop = cap_drop,
                    subnet = spec.get("subnet"),
                    host_binds = host_binds,
                 )
        return kwargs

    @cached_property
    def spec(self):
        return json.loads(self.labels["LDMSDCluster.spec"])

    @property
    def containers(self):
        s = super(LDMSDCluster, self)
        return [ LDMSDContainer(c.obj, self) for c in s.containers ]

    def get_container(self, name):
        cont = super(LDMSDCluster, self).get_container(name)
        if cont:
            cont = LDMSDContainer(cont.obj, self)
        return cont

    def start_ldmsd(self):
        """Start ldmsd in each node in the cluster"""
        for cont in self.containers:
            cont.start_ldmsd()

    def check_ldmsd(self):
        """Returns a dict(hostname:bool) indicating if each ldmsd is running"""
        return { cont.hostname : cont.check_ldmsd() \
                                        for cont in self.containers }

    @cached_property
    def slurm_version(self):
        rc, out = self.exec_run("slurmd -V")
        _slurm, _ver = out.split(' ')
        ver = tuple( int(v) for v in _ver.split('.') )
        return ver

    @property
    def slurm_conf(self):
        """Content for `/etc/slurm/slurm.conf`"""
        nodes = self.spec["nodes"]
        cpu_per_node = self.spec.get("cpu_per_node", 1)
        oversubscribe = self.spec.get("oversubscribe", "NO")
        slurm_loglevel = self.spec.get("slurm_loglevel", "info")
        slurmd_nodes = []
        slurmctld_node = None
        for node in nodes:
            daemons = node.get("daemons", [])
            daemons = set( d["type"] for d in daemons )
            if "slurmd" in daemons:
                slurmd_nodes.append(node["hostname"])
            if "slurmctld" in daemons:
                slurmctld_node = node["hostname"]
        slurmd_nodes = ",".join(slurmd_nodes)
        # Check slurmd version
        if self.slurm_version >= (18, 0, 0):
            slurmctld_key = "SlurmctldHost"
        else:
            slurmctld_key = "ControlMachine"
        slurmconf = \
            "{slurmctld_key}={slurmctld_node}\n"\
            "MpiDefault=none\n"\
            "ProctrackType=proctrack/linuxproc\n"\
            "ReturnToService=1\n"\
            "SlurmctldPidFile=/var/run/slurmctld.pid\n"\
            "SlurmctldPort=6817\n"\
            "SlurmdPidFile=/var/run/slurmd.pid\n"\
            "SlurmdPort=6818\n"\
            "SlurmdSpoolDir=/var/spool/slurmd\n"\
            "SlurmUser=root\n"\
            "StateSaveLocation=/var/spool\n"\
            "SwitchType=switch/none\n"\
            "TaskPlugin=task/none\n"\
            "TaskPluginParam=Sched\n"\
            "InactiveLimit=0\n"\
            "KillWait=30\n"\
            "MinJobAge=300\n"\
            "SlurmctldTimeout=120\n"\
            "SlurmdTimeout=300\n"\
            "Waittime=0\n"\
            "FastSchedule=1\n"\
            "SchedulerType=sched/builtin\n"\
            "SelectType=select/cons_res\n"\
            "SelectTypeParameters=CR_CPU\n"\
            "AccountingStorageType=accounting_storage/none\n"\
            "AccountingStoreJobComment=YES\n"\
            "ClusterName=cluster\n"\
            "JobCompType=jobcomp/none\n"\
            "JobAcctGatherFrequency=30\n"\
            "JobAcctGatherType=jobacct_gather/none\n"\
            "SlurmctldDebug={slurm_loglevel}\n"\
            "SlurmctldLogFile=/var/log/slurmctld.log\n"\
            "SlurmdDebug={slurm_loglevel}\n"\
            "SlurmdLogFile=/var/log/slurmd.log\n"\
            "NodeName={slurmd_nodes} CPUs={cpu_per_node} State=UNKNOWN\n"\
            "PartitionName=debug Nodes={slurmd_nodes} OverSubscribe={oversubscribe} Default=YES MaxTime=INFINITE State=UP\n"\
            "LogTimeFormat=thread_id\n"\
            .format( slurmctld_key = slurmctld_key,
                    slurmctld_node = slurmctld_node,
                    slurmd_nodes = slurmd_nodes,
                    cpu_per_node = cpu_per_node,
                    oversubscribe = oversubscribe,
                    slurm_loglevel = slurm_loglevel )
        return slurmconf

    def start_munged(self, **kwargs):
        """Start Munge Daemon"""
        for cont in self.containers:
            cont.start_munged(**kwargs)

    def start_slurm(self):
        """Start slurmd in all sampler nodes, and slurmctld on svc node"""
        self.start_munged()
        for cont in self.containers:
            cont.start_slurm()

    def start_sshd(self):
        """Start sshd in all containers"""
        for cont in self.containers:
            cont.start_sshd()
        self.make_known_hosts()

    def ssh_keyscan(self):
        """Report ssh-keyscan result of all nodes in the cluster"""
        hosts = reduce(lambda x,y: x+y,
                         ([c.hostname, c.ip_addr] + c.aliases \
                                             for c in self.containers) )
        cmd = "ssh-keyscan " + " ".join(hosts)
        cont = self.containers[-1]
        rc, out = cont.exec_run(cmd, stderr=False)
        return out

    def make_known_hosts(self):
        """Make `/root/.ssh/known_hosts` in all nodes"""
        ks = self.ssh_keyscan()
        for cont in self.containers:
            cont.exec_run("mkdir -p /root/.ssh")
            cont.write_file("/root/.ssh/known_hosts", ks)

    def make_ssh_id(self):
        """Make `/root/.ssh/id_rsa` and authorized_keys"""
        cont = self.containers[-1]
        cont.exec_run("mkdir -p /root/.ssh/")
        cont.exec_run("rm -f id_rsa id_rsa.pub", workdir="/root/.ssh/")
        cont.exec_run("ssh-keygen -q -N '' -f /root/.ssh/id_rsa")
        D.id_rsa = id_rsa = cont.read_file("/root/.ssh/id_rsa")
        D.id_rsa_pub = id_rsa_pub = cont.read_file("/root/.ssh/id_rsa.pub")
        for cont in self.containers:
            cont.exec_run("mkdir -p /root/.ssh/")
            cont.write_file("/root/.ssh/id_rsa", id_rsa)
            cont.exec_run("chmod 600 /root/.ssh/id_rsa")
            cont.write_file("/root/.ssh/id_rsa.pub", id_rsa_pub)
            cont.write_file("/root/.ssh/authorized_keys", id_rsa_pub)

    def exec_run(self, *args, **kwargs):
        """A pass-through to last_cont.exec_run()

        The `last_cont` is the last container in the virtual cluster, which does
        NOT have any `ldmsd` role (i.e. no `ldmsd` running on it). If
        `start_slurm()` was called, the `last_cont` is also the slurm head node
        where slurmctld is running.
        """
        cont = self.containers[-1]
        return cont.exec_run(*args, **kwargs)

    def sbatch(self, script_path):
        """Submits slurm batch job, and returns job_id on success

        Parameters
        ----------
        script_path : str
            The path to the script file in the service node (the last
            container).

        Returns
        -------
        jobid : int
            The JOB ID if the submission is a success.

        Raises
        ------
        RuntimeError
            If the submission failed.
        """
        _base = os.path.basename(script_path)
        _dir = os.path.dirname(script_path)
        rc, out = self.exec_run("bash -c \"sbatch {}\"".format(_base),
                                workdir = _dir)
        if rc:
            raise RuntimeError("sbatch error, rc: {}, output: {}"\
                                    .format(rc, out))
        m = re.search(r"Submitted batch job (\d+)", out)
        job_id = int(m.group(1))
        return job_id

    def squeue(self, jobid = None):
        """Execute `squeue` and parse the results

        Parameters
        ----------
        jobid : int
            The optional jobid. If not specified, the status of all jobs are
            queried.

        Returns
        -------
        [ { "KEY" : "VALUE" }, ... ]
            A list of dictionaries describing job statuses. Examples of keys are
            "JOBID", "UID", "START_TIME", "NODELIST", "WORKDIR", and "STATE".
            Please consult 'squeue(1)' manpage for more information.
        """
        cmd = "squeue -o %all"
        if jobid:
            cmd += " -j {}".format(jobid)
        rc, out = self.exec_run(cmd)
        if rc:
            raise RuntimeError("squeue error, rc: {}, output: {}" \
                                                            .format(rc, out))
        lines = out.splitlines()
        hdr = lines.pop(0)
        keys = hdr.split('|')
        return [ dict(zip(keys, l.split('|'))) for l in lines ]

    def scancel(self, jobid):
        """Cancel job"""
        cmd = "scancel {}".format(jobid)
        self.exec_run(cmd)

    def ldms_ls(self, *args):
        """Executes `ldms_ls` with *args, and returns (rc, output)"""
        cmd = "ldms_ls " + (" ".join(args))
        return self.exec_run(cmd)

    def make_ovis_env(self):
        """Make ovis environment (ld, profile)

        NOTE: We need the ld.so.conf.d/ovis.conf because the process
        initializing slurm job forked from slurmd does not inherit
        LD_LIBRARY_PATH. The /etc/profile.d/ovis.sh is for ssh session's
        convenience (making ldms binaries available for SSH session).
        """
        allhosts = set([ c.hostname for c in self.containers ])
        allhosts_txt = ' '.join(allhosts)
        for cont in self.containers:
            cont.write_file("/etc/ld.so.conf.d/ovis.conf",
                            "/opt/ovis/lib\n"
                            "/opt/ovis/lib64\n"
                            "/opt/ovis/lib/ovis-ldms\n"
                            "/opt/ovis/lib64/ovis-ldms\n"
                            )
            cont.exec_run("ldconfig")
            profile = """
                function _add() {
                    # adding VALUE into variable NAME
                    local NAME=$1
                    local VALUE=$2
                    [[ *:${!NAME}:* = *:${VALUE}:* ]] ||
                        eval export ${NAME}=${!NAME}:${VALUE}
                }

                PREFIX=/opt/ovis
                _add PATH $PREFIX/bin
                _add PATH $PREFIX/sbin
                _add LD_LIBRARY_PATH $PREFIX/lib
                _add LD_LIBRARY_PATH $PREFIX/lib64
                _add LD_LIBRARY_PATH $PREFIX/lib/ovis-ldms
                _add LD_LIBRARY_PATH $PREFIX/lib64/ovis-ldms
                _add MANPATH $PREFIX/share/man
                _add PYTHONPATH $(echo $PREFIX/lib/python*/site-packages)

                export ZAP_LIBPATH=$PREFIX/lib/ovis-ldms
                _add ZAP_LIBPATH $PREFIX/lib64/ovis-ldms
                _add ZAP_LIBPATH $PREFIX/lib/ovis-lib
                _add ZAP_LIBPATH $PREFIX/lib64/ovis-lib
                export LDMSD_PLUGIN_LIBPATH=$PREFIX/lib/ovis-ldms
                _add LDMSD_PLUGIN_LIBPATH $PREFIX/lib64/ovis-ldms
            """
            cont.write_file("/etc/profile.d/ovis.sh", profile)
            otherhosts = allhosts - set([cont.hostname])
            otherhosts_txt = ' '.join(otherhosts)
            pssh_profile = """
                export ALLHOSTS='{allhosts_txt}'
                export OTHERHOSTS='{otherhosts_txt}'
                alias pssh.others='pssh -H "${{OTHERHOSTS}}"'
                alias pscp.others='pscp.pssh -H "${{OTHERHOSTS}}"'
            """.format(
                    allhosts_txt = allhosts_txt,
                    otherhosts_txt = otherhosts_txt,
                )
            cont.write_file("/etc/profile.d/pssh.sh", pssh_profile)

    def pgrepc(self, prog):
        """Perform `cont.pgrepc(prog)` for cont in self.containers"""
        return { cont.hostname : cont.pgrepc(prog) \
                                    for cont in self.containers }

    def start_daemons(self):
        """Start daemons according to spec"""
        for cont in self.containers:
            cont.start_daemons()

    def all_exec_run(self, cmd):
        return { c.hostname: c.exec_run(cmd) for c in self.containers }

    @cached_property
    def ldmsd_version(self):
        rc, out = self.exec_run("ldmsd -V")
        _drop, _ver = out.splitlines()[0].split(': ')
        ver = tuple( int(v) for v in _ver.split('.') )
        return ver

def read_msg(_file):
    """Read a message "\x01...\x03" from `_file` file handle"""
    pos = _file.tell()
    sio = StringIO()
    c = _file.read(1)
    if not c:
        raise ValueError("End of file")
    if c != "\x01":
        _file.seek(pos)
        raise ValueError("Not a start of message")
    c = _file.read(1)
    while c and c != "\x02":
        sio.write(c)
        c = _file.read(1)
    if c != "\x02":
        _file.seek(pos)
        raise ValueError("Bad message header")
    _type = sio.getvalue()
    sio = StringIO() # reset sio
    c = _file.read(1)
    while c and c != "\x03":
        sio.write(c)
        c = _file.read(1)
    if c != "\x03":
        _file.seek(pos)
        raise ValueError("Incomplete message")
    text = sio.getvalue()
    text = text.strip('\x00')
    obj = None
    if _type == "json":
        obj = json.loads(text)
    return { "type": _type, "text": text, "obj": obj }

LDMSD_STR_VER_RE = re.compile(r'LDMSD_VERSION (\d+).(\d+).(\d+)')
LDMSD_EXE_VER_RE = re.compile(r'LDMSD Version: (\d+).(\d+).(\d+)')
def ldmsd_version(prefix):
    """Get LDMSD version from the installation prefix"""
    try:
        _cmd = "strings {}/sbin/ldmsd | grep 'LDMSD_VERSION '".format(prefix)
        out = subprocess.check_output(_cmd, shell = True).decode()
    except:
        out = ""
    m = LDMSD_STR_VER_RE.match(out)
    if not m:
        # try `ldmsd -V`
        try:
            _cmd = "{}/sbin/ldmsd -V | grep 'LDMSD Version: '".format(prefix)
            out = subprocess.check_output(_cmd, shell = True).decode()
        except:
            out = ""
        m = LDMSD_EXE_VER_RE.match(out)
        if not m:
            raise ValueError("Cannot determine ldmsd version")
    return tuple(map(int, m.groups()))

def is_ldmsd_version_4(ver):
    return ver < (4, 100, 0)

class Munged(object):
    """Munged(cont)
    Munged(cont, dom = "DOM_NAME", key = "KEY")

    A munged handler in a docker container `cont`. If `dom` is not given, use
    the default file locations. If `key` is not given, use the existing key.

    If `dom` is given, all of the files will be put under `/munge/DOM_NAME`.
    This is so that we can run multiple `munged` to serve multiple
    authentication domain.

    Examples:
    >>> m0 = Munged(cont) # default domain, use existing key
    >>> m1 = Munged(cont, dom = "dom1", key = 'x'*1024) # custom domain
    ... # using /munge/dom1 directory
    """
    def __init__(self, cont, dom = None, key = None):
        self.cont = cont
        self.dom = dom
        self.key = key
        if dom:
            self.key_file = "/munge/{}/key".format(dom)
            self.pid_file = "/munge/{}/pid".format(dom)
            self.sock_file = "/munge/{}/sock".format(dom)
        else:
            self.key_file = "/etc/munge/munge.key"
            self.pid_file = "/run/munge/munged.pid"
            self.sock_file = "/run/munge/munge.socket.2"

    def _prep_dom(self):
        cont = self.cont
        _dir = "/munge/{}".format(self.dom) if self.dom else "/run/munge"
        rc, out = cont.exec_run("mkdir -p {}".format(_dir))
        if rc:
            raise RuntimeError("Cannot create directory '{}', rc: {}, out: {}"\
                               .format(_dir, rc, out))
        self.cont.chown("munge:munge", _dir)

    def _prep_key_file(self):
        _key = self.key
        if not _key: # key not given
            rc, out = self.cont.exec_run("ls {}".format(self.key_file))
            if rc == 0: # file existed, and no key given .. use existing key
                return
            _key = "0"*4096 # use default key if key_file not existed
        self.cont.write_file(self.key_file, _key, user = "munge")
        self.cont.chmod(0o600, self.key_file)

    def get_pid(self):
        """PID of the running munged, `None` if it is not running"""
        rc, out = self.cont.exec_run("cat {}".format(self.pid_file))
        if rc:
            return None
        return int(out)

    def is_running(self):
        """Returns `True` if munged is running"""
        pid = self.get_pid()
        if not pid:
            return False
        rc, out = self.cont.exec_run("ps -p {}".format(pid))
        return rc == 0

    def start(self):
        """Start the daemon"""
        if self.is_running():
            return # do nothing
        self._prep_dom()
        self._prep_key_file()
        cmd = "munged"
        if self.dom:
            cmd += " -S {sock} --pid-file {pid} --key-file {key}"\
                   .format( sock = self.sock_file, pid = self.pid_file,
                            key = self.key_file )
        rc, out = self.cont.exec_run(cmd, user = "munge")
        if rc:
            raise RuntimeError("`{}` error, rc: {}, out: {}"\
                               .format(cmd, rc, out))

    def kill(self):
        """Kill the daemon"""
        pid = self.get_pid()
        self.cont.exec_run("kill {}".format(pid))


class ContainerTTY(object):
    """A utility to communicate with a process inside a container"""
    EOT = b'\x04' # end of transmission (ctrl-d)

    def __init__(self, sockio):
        self.sockio = sockio
        self.sock = sockio._sock
        self.sock.setblocking(False) # use non-blocking io

    def read(self, idle_timeout = 1):
        bio = BytesIO()
        active = 1
        while True:
            try:
                buff = self.sock.recv(1024)
                bio.write(buff)
                active = 1 # stay active if read succeed
            except BlockingIOError as e:
                if e.errno != errno.EAGAIN:
                    raise
                if not active: # sock stays inactive > idle_timeout
                    break
                active = 0
                time.sleep(idle_timeout)
        val = bio.getvalue()
        return val.decode() if val else None

    def write(self, data):
        if type(data) == str:
            data = data.encode()
        self.sock.send(data)

    def term(self):
        if self.sock:
            self.sock.send(self.EOT)
            self.sock = None
            self.sockio.close()

# control sequence regex
CS_RE = re.compile("""
(?:
    \x1b\\[       # ESC[ -- the control sequence introducer
    [\x30-\x3f]*  # parameter bytes
    [\x20-\x2f]*  # intermediate bytes
    [\x40-\x7e]   # final byte
)
""", re.VERBOSE)
def cs_rm(s):
    """Remove control sequences from the string `s`"""
    return CS_RE.sub("", s)

if __name__ == "__main__":
    exec(open(os.getenv('PYTHONSTARTUP', '/dev/null')).read())
