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
from ldmsd.parser_util import *

from IPython.core.debugger import set_trace

class LdmsdScaleTest(YamlCfg):
    def __init__(self, client, name, cluster_config, args):
        super().__init__(client, name, cluster_config, args)
        self.TEST_DUR = args.TEST_DUR
        self.FLAP_DUR = args.FLAP_DUR
        self.GAP_DUR = args.GAP_DUR
        self.NUM_DAEMONS = len(self.daemons)
        self.FLAP = True
        self.mem_usage = None
        if args.memory_usage:
            self.mem_usage = {}
            for ldmsd in self.daemons:
                # Constructs a dictionary of ldmsds, with the key being the
                # ldmsd name and each value being a tuple of
                # (<timestamp>, <memory_in_kb>, <%memory_utilization>, <%cpu_utilization>)
                self.mem_usage[ldmsd] = []

    def start_ldmsds(self):
        self.pid_list = {}
        for ldmsd in self.daemons:
            ep_key = next(iter(self.daemons[ldmsd]['endpoints'].keys()))
            ep = self.daemons[ldmsd]['endpoints'][ep_key]
            host = self.daemons[ldmsd]['addr']
            cmd_str = f'ldmsd -y {self.args.yaml_file} -n {ldmsd} -l '\
                        '/opt/nick/ovis/etc/log/{ldmsd}.log > /dev/null 2>&1 & echo $!'
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
            print(f'{ldmsd}')

    def get_running_daemon_count(self):
        return len(self.pid_list)

    def kill_ldmsds(self):
        n = 0
        print(f'{len(self.pid_list)}')
        if self.args.pid:
            self.pid_kill()
            return
        for grp in self.cluster_config['daemons']:
            sp = subprocess.Popen(['pdsh', '-R', 'ssh', '-w', f'{grp["hosts"]}',
                                   'pkill', 'ldmsd'])
            sp.communicate()
            sp.terminate()
            print(f'Killing ldmsds on nodes {grp["hosts"]}')

    def pid_kill(self):
        n = 0
        for ldmsd in self.daemons:
            print(f'Killing {ldmsd}')
            sp = subprocess.Popen(['ssh', f'{self.daemons[ldmsd]["addr"]}', f'kill -9 {self.pid_list[ldmsd]}'])
            sp.communicate()
            self.pid_list.pop(ldmsd)
            sp.terminate()

    def scale_test(self):
        COUNT = 0
        ELAPSED = 0
        while self.FLAP:
            print(f'=================`date`: {COUNT} -- Running for {ELAPSED}================\n')
            if self.args.debug:
                print(f'Starting...\n')
            stime = time.perf_counter()
            self.start_ldmsds()
            etime = time.perf_counter()
            dtime = etime - stime
            print(f'Startup took {dtime} seconds\n')
            num = self.get_running_daemon_count()
            print(f'Running daemons={num}\n')
            if dtime < self.GAP_DUR:
                time.sleep(self.GAP_DUR - dtime)
            else:
                time.sleep(self.GAP_DUR)
            stime = time.perf_counter()
            if self.mem_usage:
                self.get_mem_usage()
            etime = time.perf_counter()
            print(f'mem usage count took {etime - stime} seconds\n')
            stime = time.perf_counter()
            self.pid_kill()
            etime = time.perf_counter()
            print(f'Shutdown took {etime-stime}\n')
            num = self.get_running_daemon_count()
            print(f'{num}/{self.NUM_DAEMONS}\n')
            ELAPSED += self.FLAP_DUR
            COUNT += 1
            if ELAPSED >= self.TEST_DUR:
                self.FLAP = False
            time.sleep(self.GAP_DUR)
        print(f'{self.mem_usage}')
        print(f'Test finished. Exiting...')

    def get_pid(self, ldmsd):
        return self.pid_list[ldmsd]

    def get_mem_usage(self):
        for ldmsd in self.daemons:
            sp = subprocess.Popen(['ssh', f'{self.daemons[ldmsd]["addr"]}',
                                   f'ps -p {self.pid_list[ldmsd]} '\
                                    '-o rss=,%mem=,cuc='],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  text=True)
            tstamp = time.time()
            stdout, stderr = sp.communicate()
            mem_kb, mem_p, cpu = stdout.split()
            self.mem_usage[ldmsd].append((tstamp, mem_kb, mem_p, cpu))
            sp.terminate()

if __name__ == "__main__":
    # Script options
    parser = argparse.ArgumentParser(
        description='Scale testing for LDMSD')
    parser.add_argument('-L', '--TEST_DUR', type=int, default=300,
                        help='How long the test will run in seconds')
    parser.add_argument('-S', '--FLAP_DUR', type=int, default=120,
                        help='How long in seconds daemons will run in each '\
                             'flap')
    parser.add_argument('-G', '--GAP_DUR', type=int, default=1,
                        help='Duration in seconds between each flap')
    parser.add_argument('-P', '--SCRIPT_LOG_PATH',
                        help='Path to the file for the script log')
    parser.add_argument('-N', '--NUM_DAEMON', type=int, default=1,
                        help='Number of daemons')
    parser.add_argument('--debug', default=False,
                        help='Log debug messages')
    parser.add_argument('--dryrun',
                        help='Echo the ldmsd cmd-lines')
    parser.add_argument('-pid', action='store_true',
                        help='Use PID when killing daemons')
    parser.add_argument('-mem', '--memory_usage', action='store_true',
                        help='Record memory usage of LDMSDs in the cluster')
    parser.add_argument('-M', '--maestro', action='store_true',
                        help='Use maestro to configure LDMSD cluster')

    # LDMSD options
    parser.add_argument('-y', '--yaml_file', required=True,
                        help='Path to a YAML configuration file')
    #parser.add_argument('-c',
    #                    help='LDMSD configuration path. The actual path will be '\
    #                         '<CONF_PATH>/<DAEMON_NAME>.conf')
    #parser.add_argument('-C',
    #                    help='Path to a configuration file to be applied to all '\
    #                         'daemons')
    #parser.add_argument('-x', default='sock',
    #                    help='LDMSD transport')
    #parser.add_argument('-p', type=int, default=20001,
    #                    help='LDMSD port')
    #parser.add_argument('-a', default='none',
    #                    help='"ovis", "naive", "munge", "none"')
    #parser.add_argument('-A', default=None,
    #                    help='keyword=value, e.g. conf=/opt/ovis/secret.conf')
    args = parser.parse_args()
    config_fp = open(args.yaml_file)
    conf_spec = yaml.load(config_fp, Loader=yaml.BaseLoader)
    tester = LdmsdScaleTest(None, None, conf_spec, args)
    #tester.start_ldmsds()
    #tester.kill_ldmsds()
    tester.scale_test()
    sys.exit()
