#!/usr/bin/python3
import os, sys
import errno
import json
import subprocess
import re
import socket
import time
import argparse
import yaml
import logging
import random
from ldmsd.parser_util import *
import ldmsd.hostlist as hostlist
from ldmsd.ldmsd_communicator import Communicator, fmt_status

from IPython.core.debugger import set_trace

class LdmsdScaleTest(YamlCfg):
    def __init__(self, client, name, cluster_config, args):
        super().__init__(client, name, cluster_config, args)
        self.TEST_DUR = args.TEST_DUR
        self.FLAP_DUR = args.FLAP_DUR
        self.GAP_DUR = args.GAP_DUR
        self.NUM_DAEMONS = len(self.daemons)
        self.FLAP_NUM = args.flap_num
        # List of daemons that are being flapped
        self.ldmsd_flap = []
        self.fp = open(f'{args.LOG_PATH}/ldmsd_memory_metrics.json', 'w+')
        self.FLAP = True
        self.mem_usage = None
        self.maestro_args = None
        if args.maestro:
            self.maestro_args = {
                'prefix'          : f'{args.prefix}',
                'ldms_config'     : f'{args.yaml_file}',
                'cluster'         : f'{args.cluster}',
                'benchmark'       : f'{args.benchmark}',
                'loop_timeout'    : f'{args.loop_timeout}'
            }
        if args.memory_usage:
            self.mem_usage = {}
            for ldmsd in self.daemons:
                # Constructs a dictionary of ldmsds, with the key being the
                # ldmsd name and each value being a tuple of
                # (<timestamp>, <memory_in_kb>, <%memory_utilization>, <%cpu_utilization>)
                self.mem_usage[ldmsd] = []

    def start_dead_ldmsds(self, no_cfg=False):
        for ldmsd in self.ldmsd_flap:
            ep_key = next(iter(self.daemons[ldmsd]['endpoints'].keys()))
            ep = self.daemons[ldmsd]['endpoints'][ep_key]
            auth, plugin, auth_opt = check_auth(ep)
            host = self.daemons[ldmsd]['addr']
            if no_cfg:
                cmd_str = f'ldmsd -x {ep["xprt"]}:{ep["port"]}:{host} -a {plugin} '\
                          f'-l {self.args.LOG_PATH}/{ldmsd}.log > /dev/null 2>&1 & echo $!'
                if auth_opt:
                    cmd_str += f' -A {auth_opt}'
            else:
                cmd_str = f'ldmsd -y {self.args.yaml_file} -n {ldmsd} -l '\
                          f'{self.args.LOG_PATH}/{ldmsd}.log -v INFO > /dev/null '\
                          f'2>&1 & echo $!'
            sp = subprocess.Popen(['ssh', host, cmd_str],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  text=True)
            stdout, stderr = sp.communicate()
            if stderr:
                log.error(f'Error {stderr} starting {ldmsd}')
            self.pid_list[ldmsd] = stdout.strip()
            sp.terminate()
            log.info(f'{ldmsd} started')

    def start_all_ldmsd(self, no_cfg=False):
        self.pid_list = {}
        for ldmsd in self.daemons:
            ep_key = next(iter(self.daemons[ldmsd]['endpoints'].keys()))
            ep = self.daemons[ldmsd]['endpoints'][ep_key]
            host = self.daemons[ldmsd]['addr']
            # Start ldmsd without passing configration file; maestro will
            # configure the daemons
            if no_cfg:
                xprt = ep['xprt']
                port = ep['port']
                host = self.daemons[ldmsd]['addr']
                auth, plugin, auth_opt = check_auth(ep)
                cmd_str = f'ldmsd -x {ep["xprt"]}:{ep["port"]}:{host} -a {plugin} '\
                          f'-l {self.args.LOG_PATH}/{ldmsd}.log > /dev/null 2>&1 & echo $!'
                if auth_opt:
                    cmd_str += f' -A {auth_opt}'
            else:
                cmd_str = f'ldmsd -y {self.args.yaml_file} -n {ldmsd} -l '\
                          f'{self.args.LOG_PATH}/{ldmsd}.log -v INFO > /dev/null '\
                          f'2>&1 & echo $!'
                cmd_list = ['ldmsd', '-y', self.args.yaml_file, '-n', ldmsd]
            this_hostname = socket.gethostname()
            sp = None
            if host == this_hostname:
                sp = subprocess.Popen(cmd_list)
            else:
                sp = subprocess.Popen(['ssh', host, cmd_str],
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,
                                      text=True)
            stdout, stderr = sp.communicate()
            self.pid_list[ldmsd] = stdout.strip()
            sp.terminate()
            log.info(f'{ldmsd} started')

    def get_running_daemon_count(self):
        current_pids = []
        hosts = []
        for grp in self.cluster_config['daemons']:
            hosts += hostlist.expand_hostlist(grp['hosts'])
        hosts = hostlist.collect_hostlist(hosts)
        sp = subprocess.Popen(['pdsh', '-N', '-R', 'ssh', '-w', f'{hosts}',
                               'pidof', 'ldmsd'],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              text=True)
        stdout, stderr = sp.communicate()
        current_pids = stdout.split()
        last_pids = list(self.pid_list.values())
        if set(current_pids) != set(last_pids):
            for ldmsd in list(self.pid_list):
                if self.pid_list[ldmsd] not in current_pids:
                    if ldmsd not in self.ldmsd_flap:
                        self.test_fail(errno.ESRCH,
                                       f'Unexpected downed LDMSD {ldmsd} with '\
                                       f'pid {self.pid_list[ldmsd]}')
                    self.pid_list.pop(ldmsd)
        sp.terminate()
        return len(self.pid_list)

    def kill_all_ldmsd(self):
        log.info(f'{len(self.pid_list)}')
        hosts = []
        for grp in self.cluster_config['daemons']:
            hosts += hostlist.expand_hostlist(grp['hosts'])
        hosts = hostlist.collect_hostlist(hosts)
        sp = subprocess.Popen(['pdsh', '-R', 'ssh', '-w', f'{hosts}',
                               'pkill', 'ldmsd'],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              text=True)
        stdout, stderr = sp.communicate()
        sp.terminate()
        log.info(f'Killing ldmsds on nodes {hosts}')

    def pid_kill(self):
        self.ldmsd_flap = random.sample(list(self.pid_list.keys()), k=self.FLAP_NUM)
        for ldmsd in self.ldmsd_flap:
            log.info(f'Killing {ldmsd}\n')
            sp = subprocess.Popen(['ssh', f'{self.daemons[ldmsd]["addr"]}', f'kill -9 {self.pid_list[ldmsd]}'])
            sp.communicate()
            self.pid_list.pop(ldmsd)
            sp.terminate()

    def start_maestro(self):
        #Start maestro
        self.maestro_ctrl = subprocess.Popen(['maestro_ctrl',
                                              '--ldms_config', f'{self.args.yaml_file}',
                                              '--cluster', f'{self.maestro_args["cluster"]}',
                                              '--prefix', f'scale_test'],
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE,
                                             text=True
                                            )
        stdout, stderr =  self.maestro_ctrl.communicate()
        if stderr:
            log.error(f'Error in maestro_ctrl: {stderr}')
            self.pid_kill()
            sys.exit(0)
        self.maestro_ctrl.terminate()
        maestro_args = ['maestro', '--prefix', 'scale_test', '--cluster', f'{self.maestro_args["cluster"]}',
                        '--loop_timeout', f'{self.maestro_args["loop_timeout"]}',
                        f'-l', f'{self.args.LOG_PATH}/maestro.log']
        if self.args.benchmark:
            maestro_args += ['--benchmark', f'{self.maestro_args["benchmark"]}']
        self.maestro = subprocess.Popen(maestro_args,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)

    def confirm_producers(self):
        for grp in self.aggregators:
            config_grp = None
            aggs = expand_names(grp)
            for cfg_grp in self.cluster_config['aggregators']:
                if grp == cfg_grp['daemons']:
                    config_grp = cfg_grp
                    break
            peer_list = []
            # Check if producers are configured for these aggregators
            if 'peers' not in config_grp:
                continue
            for peers in config_grp['peers']:
                peer_list += expand_names(peers['daemons'])
            peer_cnt = 0
            for ldmsd in aggs:
                ep_key = next(iter(self.daemons[ldmsd]['endpoints'].keys()))
                ep = self.daemons[ldmsd]['endpoints'][ep_key]
                auth_name, plugin, auth_opt = check_auth(ep)
                comm = Communicator(ep['xprt'],
                                    self.daemons[ldmsd]['addr'],
                                    ep['port'],
                                    plugin,
                                    { 'conf' : auth_opt })
                rc = comm.connect(timeout=5)
                if rc:
                    log.info(f'Error connecting to {ldmsd}: %d', rc)
                rc, msg = comm.prdcr_status()
                comm.close()
                msg = fmt_status(msg)
                if not msg:
                    self.test_fail(rc, msg)
                peer_cnt += len(msg)
            if peer_cnt != len(peer_list):
                msg = (f'Not all producers have connected properly')
                self.test_fail(errno.ENODATA, msg)
        log.info('Producer Status OK')
        return True

    def confirm_plugins(self):
        for grp in self.stores:
            ldmsd_list = expand_names(grp)
            for ldmsd in ldmsd_list:
                ep_key = next(iter(self.daemons[ldmsd]['endpoints'].keys()))
                ep = self.daemons[ldmsd]['endpoints'][ep_key]
                auth, plugin, auth_opt = check_auth(ep)
                comm = Communicator(ep['xprt'],
                                    self.daemons[ldmsd]['addr'],
                                    ep['port'],
                                    plugin,
                                    { 'conf' : auth_opt })
                rc = comm.connect()
                rc, msg = comm.plugn_status()
                if rc:
                    print(f'Error {rc}: Error getting plugin status of {ldmsd}: '\
                          f'{msg}')
                    self.test_fail(rc, msg)
                msg = fmt_status(msg)
                store_list = []
                for store in msg:
                    store_list.append(store['name'])
                for store in self.stores[grp]:
                    pname = self.stores[grp][store]['plugin']
                    if pname not in store_list:
                        self.test_fail(errno.ENOENT, f'Plugin {store} for {ldmsd} not loaded successfully')

        for grp in self.samplers:
            ldmsd_list = expand_names(grp)
            for ldmsd in ldmsd_list:
                ep_key = next(iter(self.daemons[ldmsd]['endpoints'].keys()))
                ep = self.daemons[ldmsd]['endpoints'][ep_key]
                auth, plugin, auth_opt = check_auth(ep)
                comm = Communicator(ep['xprt'],
                                    self.daemons[ldmsd]['addr'],
                                    ep['port'],
                                    plugin,
                                    { 'conf' : auth_opt })
                comm.connect()
                rc, msg = comm.plugn_status()
                if rc:
                    print(f'Error {rc}: Error getting plugin status of {ldmsd}: '\
                          f'{msg}')
                    self.test_fail(rc, msg)
                msg = fmt_status(msg)
                plugin_list = []
                for plugn in msg:
                    plugin_list.append(plugn['name'])
                for plugin in self.samplers[grp]['plugins']:
                    if plugin not in plugin_list:
                        self.test_fail(errno.ENOENT, f'Plugin {plugin} for {ldmsd} not loaded successfully')

        return 0

    def maestro_scale_test(self):
        COUNT = 0
        ELAPSED = 0
        rc = self.start_maestro()
        while self.FLAP:
            log.info(f'=================`loop`: {COUNT} -- Running for {ELAPSED}================\n')
            if self.args.debug:
                log.info(f'Starting...\n')
            stime = time.perf_counter()
            self.start_dead_ldmsds(no_cfg=True)
            etime = time.perf_counter()
            dtime = etime - stime
            log.info(f'Startup took {dtime} seconds\n')
            if dtime < self.GAP_DUR:
                time.sleep(self.GAP_DUR - dtime)
            else:
                time.sleep(self.GAP_DUR)
            num = self.get_running_daemon_count()
            log.info(f'Running daemons={self.NUM_DAEMONS}/{num}\n')
            stime = time.perf_counter()
            if self.mem_usage:
                self.get_mem_usage()
            etime = time.perf_counter()
            log.info(f'mem usage count took {etime - stime} seconds\n')
            stime = time.perf_counter()
            self.pid_kill()
            etime = time.perf_counter()
            log.info(f'Shutdown took {etime-stime}\n')
            num = self.get_running_daemon_count()
            log.info(f'{num}/{self.NUM_DAEMONS}\n')
            ELAPSED += self.FLAP_DUR
            COUNT += 1
            if ELAPSED >= self.TEST_DUR:
                self.FLAP = False
            time.sleep(self.GAP_DUR)
        self.maestro.terminate()
        self.kill_all_ldmsd()
        self.fp.write(f'{json.dumps(self.mem_usage)}')

    def flap_test(self):
        COUNT = 0
        ELAPSED = 0
        rc = self.confirm_producers()
        rc = self.confirm_plugins()
        while self.FLAP:
            log.info(f'=================`loop`: {COUNT} -- Running for {ELAPSED}================\n')
            if self.args.debug:
                log.info(f'Starting...\n')
            stime = time.perf_counter()
            self.start_dead_ldmsds()
            etime = time.perf_counter()
            dtime = etime - stime
            log.info(f'Startup took {dtime} seconds\n')
            num = self.get_running_daemon_count()
            log.info(f'Running daemons={num}/{self.NUM_DAEMONS}\n')
            #rc = self.confirm_producers()
            if dtime < self.GAP_DUR:
                time.sleep(self.GAP_DUR - dtime)
            else:
                time.sleep(self.GAP_DUR)
            stime = time.perf_counter()
            if self.mem_usage:
                rc = self.get_mem_usage()
                if rc:
                    return 1
            etime = time.perf_counter()
            log.info(f'mem usage count took {etime - stime} seconds\n')
            stime = time.perf_counter()
            self.pid_kill()
            etime = time.perf_counter()
            log.info(f'Shutdown took {etime-stime}\n')
            num = self.get_running_daemon_count()
            log.info(f'{num}/{self.NUM_DAEMONS}\n')
            ELAPSED += self.FLAP_DUR
            COUNT += 1
            if ELAPSED >= self.TEST_DUR:
                self.FLAP = False
            time.sleep(self.GAP_DUR)
        self.fp.write(f'{json.dumps(self.mem_usage)}')
        self.kill_all_ldmsd()
        log.info(f'Test finished. Exiting...')

    def get_pid(self, ldmsd):
        return self.pid_list[ldmsd]

    def get_mem_usage(self):
        for ldmsd in self.daemons:
            sp = subprocess.Popen(['ssh', f'{self.daemons[ldmsd]["addr"]}',
                                  f'ps -p {self.pid_list[ldmsd]} '\
                                  f'-o rss=,%mem=,cuc=,vsz='],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  text=True)
            tstamp = time.time()
            stdout, stderr = sp.communicate()
            try:
                mem_kb, mem_p, cpu, vsz = stdout.split()
            except Exception as e:
                mem_kb, mem_p, cpu, vsz = 'n/a', 'n/a', 'n/a', 'n/a'
                log.info(f'Error getting ldmsd: {ldmsd} utilization data: {e}')
                sp.terminate()
                self.pid_list.pop(ldmsd)
                return 1
            self.mem_usage[ldmsd].append((tstamp, mem_kb, mem_p, cpu, vsz))
            sp.terminate()

    def test_fail(self, rc, msg):
        log.error(f'Test Failed with {rc}: {msg}')
        self.kill_all_ldmsd()
        sys.exit()

if __name__ == "__main__":
    # Script options
    parser = argparse.ArgumentParser(
        description='Scale testing for LDMSD')
    parser.add_argument('-L', '--TEST_DUR', type=int, default=300,
                        help='How long the test will run in seconds')
    parser.add_argument('-S', '--FLAP_DUR', type=int, default=120,
                        help='How long in seconds daemons will run in each '\
                             'flap')
    parser.add_argument('-P', '--SCRIPT_LOG_PATH',
                        help='Path to the file for the script log')
    parser.add_argument('-f', '--flap_num', type=int, default=10,
                        help='Number of randomly chosen daemons to flap')
    parser.add_argument('-G', '--GAP_DUR', type=int, default=1,
                        help='Duration in seconds between each flap')
    parser.add_argument('--debug', default=False,
                        help='Log debug messages')
    parser.add_argument('-mem', '--memory_usage', action='store_true',
                        help='Record memory usage of LDMSDs in the cluster')
    parser.add_argument('--log_level', default='info',
                        help='Log level (debug, info, warn, error, fatal)')

    #Maestro options
    parser.add_argument('-M', '--maestro', action='store_true',
                        help='Use maestro to configure LDMSD cluster')
    parser.add_argument('--prefix', default='scale_test',
                        help='Used by maestro and maestro_ctrl. The prefix name '\
                             'in the etcd database for this cluster configuration. '\
                             'Defaults to "scale_test"')
    parser.add_argument('--ldms_config', default='config/scale_test.yaml',
                        help='Used by maestro_ctrl. The path to the LDMSD YAML '\
                             'configuration file')
    parser.add_argument('--cluster', default='config/etcd.yaml',
                        help='Used by maestro and maestro_ctrl. The path to a '\
                             'YAML file containing IP address and port number of the '\
                             'etcd daemon. Defaults to "config/etcd.yaml"')
    parser.add_argument('--loop_timeout', default='5',
                        help='Used by maestro. The timeout of each monitoring '\
                             'loop before querying LDMS daemons for their status')
    parser.add_argument('--benchmark', default='log/benchmark.json',
                        help='Used by maestro. Path to a file to store maestro '\
                             'benchmarking data in json format.')

    # LDMSD options
    parser.add_argument('-y', '--yaml_file', required=True,
                        help='Path to a YAML configuration file')
    parser.add_argument('-l', '--LOG_PATH',
                        default='/tmp/log',
                        help='Path to directory for logs')
    args = parser.parse_args()
    config_fp = open(args.yaml_file)
    conf_spec = yaml.load(config_fp, Loader=yaml.BaseLoader)
    LOG_LEVEL_TBL = dict(
                DEBUG       = logging.DEBUG,
                INFO        = logging.INFO,
                WARN        = logging.WARN,
                WARNING     = logging.WARNING,
                ERROR       = logging.ERROR,
                FATAL       = logging.FATAL,
                CRITICAL    = logging.CRITICAL
    )
    log_level = LOG_LEVEL_TBL.get(args.log_level.upper(), 0)
    logging.basicConfig(
            format='%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s',
            datefmt='%F %T'
    )
    log = logging.getLogger(__name__)
    log.setLevel(log_level)
    tester = LdmsdScaleTest(None, None, conf_spec, args)
    if args.maestro:
        rc = tester.start_all_ldmsd(no_cfg=True)
        tester.maestro_scale_test()
    else:
        tester.start_all_ldmsd()
        rc = tester.flap_test()
        if rc:
            log.error(f'Test exited with {rc}')
    sys.exit()
