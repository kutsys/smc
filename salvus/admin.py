#!/usr/bin/env python

"""
Administration and Launch control of salvus components
"""

####################
# Standard imports
####################
import logging, os, shutil, signal, socket, stat, subprocess, tempfile, time

from string import Template

import misc

############################################################
# Paths where data and configuration are stored
############################################################
DATA   = 'data'
CONF   = 'conf'
AGENT  = os.path.join(os.environ['HOME'], '.ssh', 'agent')
PWD    = os.path.abspath('.')
PIDS   = os.path.join(DATA, 'pids')   # preferred location for pid files
LOGS   = os.path.join(DATA, 'logs')   # preferred location for pid files
BIN    = os.path.join(DATA, 'local', 'bin')
PYTHON = os.path.join(BIN, 'python')
SECRETS = os.path.join(DATA,'secrets')


# Read in socket of ssh-agent, if there is an AGENT file.
# NOTE: I'm using this right now on my laptop, but it's not yet
# deployed on cloud.sagemath *yet*.  When done, it will mean the
# ssh key used by the hub and snap is password protected, which
# will be much more secure: someone who steals ~/.ssh gets nothing,
# though still if somebody logs in as the salvus user on one of
# these nodes, they can ssh to other nodes, though they can't
# change passwords, etc.   Also, this means having the ssh private
# key on the compute vm's is no longer a security risk, since it
# is protected by a (very long, very random) passphrase.
if os.path.exists(AGENT):
    for X in open(AGENT).readlines():
        if 'SSH_AUTH_SOCK' in X:
            # The AGENT file is as output by ssh-agent.
            os.environ['SSH_AUTH_SOCK'] = X.split(';')[0][len('SSH_AUTH_SOCK='):]

# TODO: factor out all $HOME/salvus/salvus style stuff in code below and use BASE.
BASE = 'salvus/salvus/'

LOG_INTERVAL = 6

GIT_REPO=''   # TODO

whoami = os.environ['USER']

# Default ports
HAPROXY_PORT = 8000
NGINX_PORT   = 8080

HUB_PORT       = 5000
HUB_PROXY_PORT = 5001

# These are used by the firewall.
CASSANDRA_CLIENT_PORT = 9160
CASSANDRA_NATIVE_PORT = 9042
CASSANDRA_INTERNODE_PORTS = [7000, 7001]
CASSANDRA_PORTS = CASSANDRA_INTERNODE_PORTS + [CASSANDRA_CLIENT_PORT, CASSANDRA_NATIVE_PORT]

####################
# Running a subprocess
####################
def run(args, maxtime=30, verbose=True):
    """
    Run the command line specified by args (using subprocess.Popen)
    and return the stdout and stderr, killing the subprocess if it
    takes more than maxtime seconds to run.

    If args is a list of lists, run all the commands separately in the
    list.
    """
    if args and isinstance(args[0], list):
        return '\n'.join([str(run(a, maxtime=maxtime,verbose=verbose)) for a in args])

    args = [str(x) for x in args]
    def timeout(*a):
        raise KeyboardInterrupt("running '%s' took more than %s seconds, so killed"%(' '.join(args), maxtime))
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(maxtime)
    if verbose:
        log.info("running '%s'", ' '.join(args))
    try:
        out = subprocess.Popen(args, stdin=subprocess.PIPE, stdout = subprocess.PIPE,
                                stderr=subprocess.PIPE).stdout.read()
        if False and verbose:
            log.info("output '%s'", out)
        return out
    finally:
        signal.signal(signal.SIGALRM, signal.SIG_IGN)  # cancel the alarm

# A convenience object "sh":
#      sh['list', 'of', ..., 'arguments'] to run a shell command

class SH(object):
    def __getitem__(self, args):
        return run([args] if isinstance(args, str) else list(args))
sh = SH()

def process_status(pid, run):
    """
    Return the status of a process, obtained using the ps command.
    The run option is used to run the command (so it could run on
    a remote machine).  The result is a dictionary; it is empty if
    the given process is not running.
    """
    fields = ['%cpu', '%mem', 'etime', 'pid', 'start', 'cputime', 'rss', 'vsize']
    v = run(['ps', '-p', str(int(pid)), '-o', ' '.join(fields)], verbose=False).splitlines()
    if len(v) <= 1: return {}
    return dict(zip(fields, v[-1].split()))

########################################
# Standard Python Logging
########################################
logging.basicConfig()
log = logging.getLogger('')
#log.setLevel(logging.DEBUG)   # WARNING, INFO, etc.
#log.setLevel(logging.WARNING)   # WARNING, INFO, etc.
log.setLevel(logging.INFO)   # WARNING, INFO, etc.

def restrict(path):
    #log.info("ensuring that '%s' has restrictive permissions", path)
    if os.stat(path)[stat.ST_MODE] != 0o40700:
        os.chmod(path, 0o40700)

def init_data_directory():
    #log.info("ensuring that '%s' exist", DATA)

    for path in [DATA, PIDS, LOGS]:
        if not os.path.exists(path):
            os.makedirs(path)
        restrict(path)

    #log.info("ensuring that PATH starts with programs in DATA directory")
    os.environ['PATH'] = os.path.join(DATA, 'local/bin/') + ':' + os.environ['PATH']

init_data_directory()

########################################
# Misc operating system interaction
########################################
def system(args):
    """
    Run the command line specified by args (using os.system) and
    return the stdout and stderr, killing the subprocess if it takes
    more than maxtime seconds to run.  If args is a list of lists, run
    all the commands separately in the list, returning *sum* of error
    codes output by os.system.
    """
    if args and isinstance(args[0], list):
        return sum([system(a) for a in args])

    c = ' '.join([str(x) for x in args])
    log.info("running '%s' via system", c)
    return os.system(c)

def abspath(path='.'):
    return os.path.abspath(path)

def kill(pid, signal=15):
    """Send signal to the process with pid."""
    if pid is not None:
        return run(['kill', '-%s'%signal, pid])

def copyfile(src, target):
    return shutil.copyfile(src, target)

def readfile(filename):
    """Read the named file and return its contents."""
    if not os.path.exists(filename):
        raise IOError, "no such file or directory: '%s'"%filename
    try:
        return open(filename).read()
    except IOError:
        pass

def writefile(filename, content):
    open(filename,'w').write(content)

def makedirs(path):
    if not os.path.exists(path):
        os.makedirs(path)

def unlink(filename):
    os.unlink(filename)

def path_exists(path):
    return os.path.exists(path)

def is_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

########################################
# Component: named collection of Process objects
########################################

class Component(object):
    def __init__(self, id, processes):
        self._processes = processes
        self._id = id

    def __repr__(self):
        return "Component %s with %s processes"%(self._id, len(self._processes))

    def __getitem__(self, i):
        return self._processes[i]

    def _procs_with_id(self, ids):
        return [p for p in self._processes if ids is None or p.id() in ids]

    def start(self, ids=None):
        return [p.start() for p in self._procs_with_id(ids)]

    def stop(self, ids=None):
        return [p.stop() for p in self._procs_with_id(ids)]

    def reload(self, ids=None):
        return [p.reload() for p in self._procs_with_id(ids)]

    def restart(self, ids=None):
        return [p.restart() for p in self._procs_with_id(ids)]

    def status(self, ids=None):
        return [p.status() for p in self._procs_with_id(ids)]


########################################
# Process: a daemon process
########################################

class Process(object):
    def __init__(self, id, name, port,
                 pidfile, logfile=None, monitor_database=None,
                 start_cmd=None, stop_cmd=None, reload_cmd=None,
                 start_using_system = False,
                 service=None,
                 term_signal=15):
        self._name = name
        self._port = port
        self._id = str(id)
        assert len(self._id.split()) == 1
        self._pidfile = pidfile
        self._start_cmd = start_cmd
        self._start_using_system = start_using_system
        self._stop_cmd = stop_cmd
        self._reload_cmd = reload_cmd
        self._pids = {}
        self._logfile = logfile
        self._monitor_database = monitor_database
        self._monitor_pidfile = os.path.splitext(pidfile)[0] + '-log.pid'
        self._term_signal = term_signal

    def id(self):
        return self._id

    def log_tail(self):
        if self._logfile is None:
            raise NotImplementedError("the logfile is not known")
        system(['tail', '-f', self._logfile])

    def _parse_pidfile(self, contents):
        return int(contents)

    def _read_pid(self, file):
        try:
            return self._pids[file]
        except KeyError:
            try:
                self._pids[file] = self._parse_pidfile(readfile(file).strip())
            except IOError: # no file
                self._pids[file] = None
        return self._pids[file]

    def pid(self):
        return self._read_pid(self._pidfile)

    def is_running(self):
        return len(self.status()) > 0

    def _start_monitor(self):
        # TODO: temporarily disabled -- they do no real good anyways.
        return
        if self._monitor_database and self._logfile:
            run([PYTHON, 'monitor.py', '--logfile', self._logfile,
                 '--pidfile', self._monitor_pidfile, '--interval', LOG_INTERVAL,
                 '--database_nodes', self._monitor_database,
                 '--target_pidfile', self._pidfile,
                 '--target_name', self._name,
                 '--target_address', socket.gethostname(),
                 '--target_port', self._port])

    def monitor_pid(self):
        return self._read_pid(self._monitor_pidfile)

    def _stop_monitor(self):
        # NOTE: This function should never need to be called; the
        # monitor stops automatically when the process it is
        # monitoring stops and it has succeeded in recording this fact
        # in the database.
        if self._monitor_database and self._logfile and path_exists(self._monitor_pidfile):
            try:
                kill(self.monitor_pid())
                unlink(self._monitor_pidfile)
            except Exception, msg:
                print msg

    def _pre_start(self):
        pass # overload to add extra config steps before start

    def start(self):
        if self.is_running(): return
        self._pids = {}
        self._pre_start()
        if self._start_cmd is not None:
            if self._start_using_system:
                print system(self._start_cmd)
            else:
                print run(self._start_cmd)
        print self._start_monitor()

    def stop(self):
        pid = self.pid()
        if pid is None: return
        if self._stop_cmd is not None:
            print run(self._stop_cmd)
        else:
            kill(pid, self._term_signal)
        try:
            if os.path.exists(self._pidfile): unlink(self._pidfile)
        except Exception, msg:
            print msg

        while True:
            s = process_status(pid, run)
            if not s:
                break
            print "waiting for %s to terminate"%pid
            time.sleep(0.5)

        self._pids = {}

    def reload(self):
        self._stop_monitor()
        self._pids = {}
        if self._reload_cmd is not None:
            return run(self._reload_cmd)
        else:
            return 'reload not defined'

    def status(self):
        pid = self.pid()
        if not pid: return {}
        s = process_status(pid, run)
        if not s:
            self._stop_monitor()
            self._pids = {}
            if path_exists(self._pidfile):
                unlink(self._pidfile)
        return s

    def restart(self):
        self.stop()
        self.start()


####################
# Nginx
####################
class Nginx(Process):
    def __init__(self, id=0, port=NGINX_PORT, monitor_database=None, base_url=""):
        self._base_url = base_url
        self._port = port
        self._log = 'nginx-%s.log'%id
        self._pid = 'nginx-%s.pid'%id
        self._nginx_conf = 'nginx-%s.conf'%id
        nginx_cmd = ['nginx', '-c', '../' + self._nginx_conf]
        Process.__init__(self, id, name='nginx', port=self._port,
                         monitor_database = monitor_database,
                         logfile   = os.path.join(LOGS, self._log),
                         pidfile    = os.path.join(PIDS, self._pid),
                         start_cmd  = nginx_cmd,
                         stop_cmd   = nginx_cmd + ['-s', 'stop'],
                         reload_cmd = nginx_cmd + ['-s', 'reload'])

    def _pre_start(self):
        # Create and write conf file
        conf = Template(open(os.path.join(CONF, 'nginx.conf')).read())
        conf = conf.substitute(logfile=self._log, pidfile=self._pid,
                               http_port=self._port, base_url=self._base_url,
                               ifbase='#' if not self._base_url else '',
                               ifnobase='' if not self._base_url else '#')
        writefile(filename=os.path.join(DATA, self._nginx_conf), content=conf)

        # Write base_url javascript file, so clients have access to it
        s = "salvus_base_url='%s'; /* autogenerated on nginx startup by admin.py */"%self._base_url
        open(os.path.join(PWD, 'static/salvus_base_url.js'), 'w').write(s)

    def __repr__(self):
        return "Nginx process %s"%self._id

####################
# Stunnel
####################
class Stunnel(Process):
    def __init__(self, id=0, accept_port=443, connect_port=HAPROXY_PORT, monitor_database=None):
        logfile = os.path.join(LOGS,'stunnel-%s.log'%id)
        base = abspath()
        pidfile = os.path.join(base, PIDS,'stunnel-%s.pid'%id) # abspath of pidfile required by stunnel
        self._stunnel_conf = os.path.join(DATA, 'stunnel-%s.conf'%id)
        self._accept_port = accept_port
        self._connect_port = connect_port
        Process.__init__(self, id, name='stunnel', port=accept_port,
                         monitor_database = monitor_database,
                         logfile    = logfile,
                         pidfile    = pidfile,
                         # stunnel typically run as sudo, and sudo need not preserve PATH on Linux.
                         start_cmd  = [os.path.join(base, DATA, 'local/bin', 'stunnel'), self._stunnel_conf])

    def _pre_start(self):
        pem = os.path.join(SECRETS, 'sagemath.com/nopassphrase.pem')
        if not os.path.exists(pem):
            raise RuntimeError("stunnel requires that the secret '%s' exists"%pem)

        stunnel = 'stunnel.conf'
        conf = Template(open(os.path.join(CONF, stunnel)).read())
        conf = conf.substitute(logfile=self._logfile, pidfile=self._pidfile,
                               accept_port=self._accept_port, connect_port=self._connect_port)
        writefile(filename=self._stunnel_conf, content=conf)

    def __repr__(self):
        return "Stunnel process %s"%self._id

####################
# HAproxy
####################
class Haproxy(Process):
    def __init__(self, id=0,
                 sitename='cloud.sagemath.com',   # name of site, e.g., 'codethyme.com' if site is https://codethyme.com; used only if insecure_redirect is set
                 accept_proxy_port=HAPROXY_PORT,  # port that stunnel sends decrypted traffic to
                 insecure_redirect_port=None,    # if set to a port number (say 80), then all traffic to that port is immediately redirected to the secure site
                 insecure_testing_port=None, # if set to a port, then gives direct insecure access to full site
                 nginx_servers=None,   # list of ip addresses
                 hub_servers=None, # list of ip addresses
                 proxy_servers=None, # list of ip addresses
                 monitor_database=None,
                 conf_file='conf/haproxy.conf',
                 base_url=''):

        pidfile = os.path.join(PIDS, 'haproxy-%s.pid'%id)
        logfile = os.path.join(LOGS, 'haproxy-%s.log'%id)

        # randomize the order of the servers to get better distribution between them by all the different
        # haproxies that are running on the edge machines. (Users may hit any of them.)
        import random

        if nginx_servers:
            random.shuffle(nginx_servers)
            t = Template('server nginx$n $ip:$port maxconn $maxconn check ')
            nginx_servers = '    ' + ('\n    '.join([t.substitute(n=n, ip=x['ip'], port=x.get('port', NGINX_PORT), maxconn=x.get('maxconn',10000)) for
                                                     n, x in enumerate(nginx_servers)]))

        if hub_servers:
            random.shuffle(hub_servers)
            t = Template('server hub$n $ip:$port cookie server:$ip:$port check inter 4000 maxconn $maxconn')
            hub_servers = '    ' + ('\n    '.join([t.substitute(n=n, ip=x['ip'], port=x.get('port', HUB_PORT), maxconn=x.get('maxconn',100)) for
                                                     n, x in enumerate(hub_servers)]))

        if proxy_servers:
            random.shuffle(proxy_servers)
            t = Template('server proxy$n $ip:$port cookie server:$ip:$port check inter 4000 maxconn $maxconn')
            proxy_servers = '    ' + ('\n    '.join([t.substitute(n=n, ip=x['ip'], port=x.get('proxy_port', HUB_PROXY_PORT), maxconn=x.get('maxconn',100)) for
                                                     n, x in enumerate(proxy_servers)]))

        if insecure_redirect_port:
            insecure_redirect = Template(
"""
frontend unsecured *:$port
    redirect location https://$sitename
""").substitute(port=insecure_redirect_port, sitename=sitename)
        else:
            insecure_redirect=''

        conf = Template(open(conf_file).read()).substitute(
            accept_proxy_port     = accept_proxy_port,
            insecure_testing_bind = 'bind *:%s'%insecure_testing_port if insecure_testing_port else '',
            nginx_servers         = nginx_servers,
            hub_servers           = hub_servers,
            proxy_servers         = proxy_servers,
            insecure_redirect     = insecure_redirect,
            base_url              = base_url
            )

        haproxy_conf = 'haproxy-%s.conf'%id
        target_conf = os.path.join(DATA, haproxy_conf)
        writefile(filename=target_conf, content=conf)
        Process.__init__(self, id, name='haproxy', port=accept_proxy_port,
                         pidfile = pidfile,
                         logfile = logfile, monitor_database = monitor_database,
                         start_using_system = True,
                         start_cmd = ['HAPROXY_LOGFILE='+logfile, os.path.join(BIN, 'haproxy'), '-D', '-f', target_conf, '-p', pidfile])

    def _parse_pidfile(self, contents):
        return int(contents.splitlines()[0])



####################
# Hub
####################
class Hub(Process):
    def __init__(self, id=0, host='', port=HUB_PORT, proxy_port=HUB_PROXY_PORT,
                 monitor_database=None, keyspace='salvus', debug=False,
                 logfile=None, pidfile=None, base_url=None, local=False):
        self._port = port
        if pidfile is None:
            pidfile = os.path.join(PIDS, 'hub-%s.pid'%id)
        if logfile is None:
            logfile = os.path.join(LOGS, 'hub-%s.log'%id)
        extra = []
        if debug:
            extra.append('-g')
        if base_url:
            extra.append('--base_url')
            extra.append(base_url)
        if local:
            extra.append('--local')
        Process.__init__(self, id, name='hub', port=port,
                         pidfile = pidfile,
                         logfile = logfile, monitor_database=monitor_database,
                         start_cmd = [os.path.join(PWD, 'hub'), 'start',
                                      '--id', id,
                                      '--port', port,
                                      '--proxy_port', proxy_port,
                                      '--keyspace', keyspace,
                                      '--host', host,
                                      '--database_nodes', monitor_database,
                                      '--pidfile', pidfile,
                                      '--logfile', logfile] + extra,
                         stop_cmd   = [os.path.join(PWD, 'hub'), 'stop', '--id', id],
                         reload_cmd = [os.path.join(PWD, 'hub'), 'restart', '--id', id])

    def __repr__(self):
        return "Hub server %s on port %s"%(self.id(), self._port)


####################
# Snap -- snapshot/backup servers
####################
class Snap(Process):
    def __init__(self, id=0, host='', monitor_database=None, keyspace='salvus',
                 snap_dir=None, logfile=None, pidfile=None, resend_all_commits=False,
                 snap_interval=None):
        if pidfile is None:
            pidfile = os.path.join(PIDS, 'snap-%s.pid'%id)
        if logfile is None:
            logfile = os.path.join(LOGS, 'snap-%s.log'%id)

        if snap_dir is None:
            snap_dir = os.path.join(DATA, 'snap-%s'%id)

        start_cmd = [os.path.join(PWD, 'snap'),
                                      'start',
                                      '--host', host,
                                      '--database_nodes', monitor_database,
                                      #'--resend_all_commits', resend_all_commits,
                                      '--keyspace', keyspace,
                                      '--snap_dir', snap_dir,
                                      '--pidfile', pidfile,
                                      '--logfile', logfile]
        if snap_interval is not None:
            start_cmd += ["--snap_interval", str(snap_interval)]

        Process.__init__(self, id, name='snap', port=0,
                         pidfile = pidfile,
                         logfile = logfile,
                         start_cmd = start_cmd,
                         stop_cmd   = [os.path.join(PWD, 'snap'), 'stop'],
                         reload_cmd = [os.path.join(PWD, 'snap'), 'restart'])

    def __repr__(self):
        return "Snap server (id=%s)"%(self.id(),)




####################
# Compute Server
####################

class Compute(Process):
    def __init__(self, id=0, host=''):
        self._port = 22
        Process.__init__(self, id,
                         name        = 'compute',
                         port        = port,
                         pidfile     = os.path.join(PIDS, 'compute_server.pid'),
                         logfile     = os.path.join(LOGS, 'compute_server.log'),
                         start_cmd   = ['compute_server', 'start'],
                         stop_cmd    = ['compute_server', 'stop'],
                         reload_cmd  = ['compute_server', 'restart'],
                         service     = ('compute', port)
        )

    def port(self):
        return self._port

########################################
# Cassandra database server
########################################
# environ variable for conf/ dir:  CASSANDRA_CONF

class Cassandra(Process):
    def __init__(self, topology=None, path=None, id=0, monitor_database=None, conf_template_path=None,
                 MAX_HEAP_SIZE=None,  HEAP_NEWSIZE=None,
                 **kwds):
        """
        id -- arbitrary identifier
        conf_template_path -- path that contains the initial conf files

        MAX_HEAP_SIZE, HEAP_NEWSIZE -- use these if your computer has a lot of memory,
        but you don't want to devote very much to cassandra; if you don't constrain it
        this way, Cassandra uses its own internal memory.  E.g., on a large-memory
        machine with strong per-user limits, use
                MAX_HEAP_SIZE="4G",  HEAP_NEWSIZE="800M"
        """

        if MAX_HEAP_SIZE is not None:
            os.environ['MAX_HEAP_SIZE'] = MAX_HEAP_SIZE
        if HEAP_NEWSIZE is not None:
            os.environ['HEAP_NEWSIZE'] = HEAP_NEWSIZE

        cassandra_install = os.path.join(DATA, 'local', 'cassandra')
        if conf_template_path is None:
            conf_template_path = os.path.join(cassandra_install, 'conf')
        assert os.path.exists(conf_template_path)

        path = os.path.join(DATA, 'cassandra-%s'%id) if path is None else path
        makedirs(path)
        log_path = os.path.join(path, 'log'); makedirs(log_path)
        lib_path = os.path.join(path, 'lib'); makedirs(lib_path)
        conf_path = os.path.join(path, 'conf'); makedirs(conf_path)

        if topology:
            kwds['endpoint_snitch'] = 'org.apache.cassandra.locator.PropertyFileSnitch'
            kwds['class_name'] = 'org.apache.cassandra.locator.SimpleSeedProvider'

        for name in os.listdir(conf_template_path):
            f = os.path.join(conf_template_path, name)
            if not os.path.isfile(f): continue
            r = open(f).read()
            r = r.replace('/var/log/cassandra', log_path)
            r = r.replace('/var/lib/cassandra', lib_path)

            if name == 'cassandra.yaml':
                for k,v in kwds.iteritems():
                    i = r.find('%s:'%k)
                    if i == -1:
                        raise ValueError("no configuration option '%s'"%k)
                    if r[i-2] == "#":
                        i = i - 2

                    j = r[i:].find('\n')
                    if j == -1:
                        j = len(r)
                    r = r[:i] + '%s: %s'%(k,v) + r[j+i:]

                if 'initial_token' not in kwds:
                    # Make sure initial_token is not set.
                    r = r.replace("\ninitial_token:","\n#initial_token:")

            elif topology and name == 'cassandra-topology.properties':

                r = topology

            writefile(filename=os.path.join(conf_path, name), content=r)

        pidfile = os.path.join(PIDS, 'cassandra-%s.pid'%id)
        Process.__init__(self, id=id, name='cassandra', port=9160,
                         logfile = '%s/system.log'%log_path,
                         pidfile = pidfile,
                         start_cmd = ['start-cassandra',  '-c', conf_path, '-p', pidfile],
                         monitor_database=monitor_database)


##############################################
# A Virtual Machine
##############################################
class Vm(Process):
    def __init__(self, ip_address, hostname=None, vcpus=2, ram=4, vnc=0, vm_type='kvm', disk='', base='salvus', id=0, monitor_database=None, name='virtual_machine'):
        """
        INPUT:

            - ip_address -- ip_address machine gets on the VPN
            - hostname -- hostname to set on the machine itself (if
              not given, sets to something based on the ip address)
            - vcpus -- number of cpus
            - ram -- number of gigabytes of ram (an integer)
            - vnc -- port of vnc console (default: 0 for no vnc)
            - vm_type -- 'kvm' (later maybe 'virtualbox'?)
            - disk -- string 'name1:size1,name2:size2,...' with size in gigabytes
            - base -- string (default: 'salvus'); name of base vm image
            - id -- optional, defaulta:0 (basically ignored)
            - monitor_database -- default: None
            - name -- default: "virtual_machine"
        """
        self._ip_address = ip_address
        self._hostname = hostname
        self._vcpus = vcpus
        self._ram = ram
        self._vnc = vnc
        self._vm_type = vm_type
        self._base = base
        self._disk = disk
        pidfile = os.path.join(PIDS, 'vm-%s.pid'%ip_address)
        logfile = os.path.join(LOGS, 'vm-%s.log'%ip_address)

        start_cmd = [PYTHON, 'vm.py', '-d', '--ip_address', ip_address,
                     '--pidfile', pidfile, '--logfile', logfile,
                     '--vcpus', vcpus, '--ram', ram,
                     '--vnc', vnc,
                     '--vm_type', vm_type, '--base', base] + \
                     (['--disk', disk] if self._disk else []) + \
                     (['--hostname', self._hostname] if self._hostname else [])

        Process.__init__(self, id=id, name=name, port=0,
                         pidfile = pidfile, logfile = logfile,
                         start_cmd = start_cmd,
                         monitor_database=monitor_database,
                         term_signal = 2   # must use 2 (=SIGINT) instead of 15 or 9 for proper cleanup!
                         )

#################################
# Classical Sage Notebook Server
#################################

class Sagenb(Process):
    def __init__(self, address, path, port, pool_size=16, monitor_database=None, debug=True, id=0):
        self._address = address  # to listen on
        self._path = path
        self._port = port
        pidfile = os.path.join(PIDS, 'sagenb-%s.pid'%port)
        logfile = os.path.join(LOGS, 'sagenb-%s.log'%port)
        Process.__init__(self, id, name='sage', port=port,
                         pidfile = pidfile,
                         logfile = logfile,
                         monitor_database = monitor_database,
                         start_cmd  = [PYTHON, 'sagenb_server.py', '--daemon',
                                       '--path', path,
                                       '--port', port,
                                       '--address', address,
                                       '--pool_size', pool_size,
                                       '--pidfile', pidfile,
                                       '--logfile', logfile]
                         )



########################################
# tinc VPN management
########################################

def ping(hostname, count=3, timeout=2):
    """
    Try to ping hostname count times, timing out if we do not
    finishing after timeout seconds.

    Return False if the ping fails.  If the ping succeeds, return
    (min, average, max) ping times in milliseconds.
    """
    p = subprocess.Popen(['ping', '-t', str(timeout), '-c', str(count), hostname],
                            stdin=subprocess.PIPE, stdout = subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if p.wait() == 0:
        r = p.stdout.read()
        i = r.rfind('=')
        v = [float(t) for t in r[i+1:].strip().split()[0].split('/')]
        return v[0], v[1], v[2]
    else:
        return False # fail

def tinc_conf(ip_address):
    """
    Configure tinc on this machine, so it can be part of the VPN.

       -- ip_address -- address this machine gets on the vpn
    """
    SALVUS = os.path.realpath(__file__)
    os.chdir(os.path.split(SALVUS)[0])

    # make sure the directories are there
    TARGET = 'data/local/etc/tinc'
    if os.path.exists(TARGET):
        print "deleting '%s'"%TARGET
        shutil.rmtree(TARGET)

    for path in [TARGET,  'data/local/var/run']:  # .../run used for pidfile
        if not os.path.exists(path):
            os.makedirs(path)

    # create symbolic link to hosts directory in salvus git repo
    os.symlink(os.path.join('../../../../conf/tinc_hosts'),
               os.path.join(TARGET, 'hosts'))

    # determine what our external ip address is
    external_ip = misc.local_ip_address(dest='8.8.8.8')

    # determine our hostname
    hostname = socket.gethostname()

    # Create the tinc-up script
    tinc_up = os.path.join(TARGET, 'tinc-up')
    open(tinc_up,'w').write(
"""#!/bin/sh
ifconfig $INTERFACE %s netmask 255.0.0.0
"""%ip_address)
    os.chmod(tinc_up, stat.S_IRWXU)

    # Create tinc.conf
    tinc_conf = open(os.path.join(TARGET, 'tinc.conf'),'w')
    tinc_conf.write('Name = %s\n'%hostname)
    for h in os.listdir(os.path.join(TARGET, 'hosts')):
        if "Address" in open(os.path.join(TARGET, 'hosts', h)).read():
            tinc_conf.write('ConnectTo = %s\n'%h)
    # on OS X, we need this, but otherwise we don't:
    if os.uname()[0] == "Darwin":
        tinc_conf.write('Device = /dev/tap0\n')
    tinc_conf.close()

    host_file = os.path.join(TARGET, 'hosts', hostname)
    open(host_file,'w').write(
"""Address = %s
Subnet = %s/32"""%(external_ip, ip_address))

    # generate keys
    print sh['data/local/sbin/tincd', '-K']

    # add file to git and checkin, then push to official repo
    gitaddr = "git@github.com:williamstein/salvus.git"
    print sh['git', 'pull', gitaddr]
    print sh['git', 'add', os.path.join('conf/tinc_hosts', hostname)]
    print sh['git', 'commit', '-a', '-m', 'tinc config for %s'%hostname]
    print sh['git', 'push', gitaddr]

    print "To join the vpn on startup,"
    print "add this line to /etc/rc.local:\n"
    print "  nice --19 /home/salvus/salvus/salvus/data/local/sbin/tincd"
    print "You *must* also pull the git repo on"
    print "at least one of the ConnectTo machines to connect."


########################################
# Grouped collection of hosts
# See the files conf/hosts* for examples.
# The format is
#   [group1]
#   hostname1
#   hostname2
#   [group2]
#   hostname3
#   hostname1  # repeats allowed, comments allowed
########################################

def parse_groupfile(filename):
    groups = {None:[]}
    group = None
    group_opts = []
    ordered_group_names = []
    namespace = {}
    for r in open(filename).xreadlines():
        line = r.split('#')[0].strip()  # ignore comments and leading/trailing whitespace
        if line: # ignore blank lines
            if line.startswith('import ') or '=' in line:
                # import modules for use in assignments below below
                print "exec ", line
                exec line in namespace
                continue

            i = line.find(' ')
            if i == -1:
                opts = {}
                name = line
            else:
                name = line[:i]
                opts = eval(line[i+1:], namespace)
            if name.startswith('['):  # host group
                group = name.strip(' []')
                group_opts = opts
                groups[group] = []
                ordered_group_names.append(group)
            else:
                opts.update(group_opts)
                groups[group].append((name, opts))
    return groups, ordered_group_names

def parse_hosts_file(filename):
    ip = {}  # ip = dictionary mapping from hostname to a list of ip addresses
    hn = {}  # hn = canonical hostnames for each ip address
    for r in open(filename).readlines():
        line = r.split('#')[0].strip()  # ignore comments and leading/trailing whitespace
        v = line.split()
        if len(v) == 0: continue
        if len(v) <= 1:
            raise ValueError("parsing hosts file -- invalid line '%s'"%r)
        address = v[0]
        hostnames = v[1:]
        hn[address] = hostnames[-1]
        for h in hostnames:
            if len(h) < 1 or len(h) > 63 or not (h.replace('-','').isalnum()):
                raise RuntimeError("invalid hostname: must be at most 63 characters from a-z, 0-9, or -")
            if h in ip:
                ip[h].append(address)
            else:
                ip[h] = [address]
    # make ip address lists canonical
    ip = dict([(host, list(sorted(set(addresses)))) for host, addresses in ip.iteritems()])
    return ip, hn

class Hosts(object):
    """
    Defines a set of hosts on a network and provides convenient tools
    for running commands on them using ssh.
    """
    def __init__(self, hosts_file, username=whoami, passwd=True):
        """
        - passwd -- if False, don't ask for a password; in this case nothing must require sudo to
          run, and all logins must work using ssh with keys
        """
        self._ssh = {}
        self._username = username
        self._password = None
        self._passwd = passwd
        self._ip_addresses, self._canonical_hostnames = parse_hosts_file(hosts_file)

    def __getitem__(self, hostname):
        """
        Return list of dinstinct ip_address matching the given hostname.  If the hostname
        is an ip address defined in the hosts file, return [hostname].
        """
        v = hostname.split()
        if len(v) > 1:
            return list(sorted(set(sum([self[q] for q in v], []))))
        if hostname in self._canonical_hostnames.keys():   # it is already a known ip address
            return [hostname]
        if hostname == 'all': # return all ip addresses
            return list(sorted(self._canonical_hostnames.keys()))
        if hostname in self._ip_addresses:
            return self._ip_addresses[hostname]
        raise ValueError("unknown ip hostname or address '%s'"%hostname)

    def hostname(self, ip):
        return self._canonical_hostnames[ip]

    def is_valid_hostname(self, hostname):
        return hostname in self._canonical_hostnames   # ok, since is dictionary mapping hostnames to canonical ones

    def password(self, retry=False):
        if not self._passwd:
            log.info("Explicitly skipping asking for password, due to passwd=False option.")
            return self._password
        if self._password is None or retry:
            import getpass
            self._password = getpass.getpass("%s's password: "%self._username)
        return self._password

    def ssh(self, hostname, timeout=20, keepalive=None, use_cache=True):
        key = (hostname, self._username)
        if use_cache and key in self._ssh:
            return self._ssh[key]
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(hostname=hostname, username=self._username, password=self._password, timeout=timeout)
        except paramiko.AuthenticationException:
            while True:
                try:
                    ssh.connect(hostname=hostname, username=self._username, password=self.password(retry=True))
                    break
                except paramiko.AuthenticationException, msg:
                    print msg
        if keepalive:
            ssh.get_transport().set_keepalive(keepalive)
        self._ssh[key] = ssh
        return ssh

    def _do_map(self, callable, address, **kwds):
        log.info('%s (%s):', address, self.hostname(address))
        x = callable(address, **kwds)
        log.info(x)
        return x

    def map(self, callable, hostname, parallel=True, **kwds):
        # needed before parallel
        self.password()
        def f(address, **kwds):
            return ((address, self.hostname(address)), self._do_map(callable, address, **kwds))
        if parallel:
            return misc.thread_map(f, [((address,), kwds) for address in self[hostname]])
        else:
            return [f(address, **kwds) for address in self[hostname]]

    def ping(self, hostname='all', timeout=3, count=3, parallel=True):
        """
        Return list of pairs ((ip, hostname), ping_time) of those that succeed at pinging
        and a list of pairs ((ip, hostname), False) for those that do not.
        """
        v = self.map(ping, hostname, timeout=timeout, count=count, parallel=parallel)
        return [x for x in v if x[1] is not False], [x for x in v if x[1] is False]

    def ip_addresses(self, hostname):
        return [socket.gethostbyname(h) for h in self[hostname]]

    def exec_command(self, hostname, command, sudo=False, timeout=20, wait=True, parallel=True, verbose=True):
        def f(hostname):
            try:
                return self._exec_command(command, hostname, sudo=sudo, timeout=timeout, wait=wait, verbose=verbose)
            except Exception, msg:
                return {'stdout':'', 'stderr':'Error connecting -- %s: %s'%(hostname, msg)}
        return dict(self.map(f, hostname=hostname, parallel=parallel))

    def __call__(self, *args, **kwds):
        """
        >>> self(hostname, command)
        """
        result = self.exec_command(*args, **kwds)
        if kwds.get('verbose',True):
            for h,v in result.iteritems():
                print '%s :'%(h,),
                print v.get('stdout',''),
                print v.get('stderr',''),
                print
        return result

    def _exec_command(self, command, hostname, sudo, timeout, wait, verbose=True):
        if not self._passwd:
            # never use sudo if self._passwd is false...
            sudo = False
        start = time.time()
        ssh = self.ssh(hostname, timeout=timeout)
        import paramiko
        try:
            chan = ssh.get_transport().open_session()
        except:
            # try again in case if remote machine got rebooted or something...
            chan = self.ssh(hostname, timeout=timeout, use_cache=False).get_transport().open_session()
        stdin = chan.makefile('wb')
        stdout = chan.makefile('rb')
        stderr = chan.makefile_stderr('rb')
        cmd = ('sudo -S bash -c "%s"' % command.replace('"', '\\"')) if sudo  else command
        log.info("hostname=%s, command='%s'", hostname, cmd)
        chan.exec_command(cmd)
        if sudo and not stdin.channel.closed:
            try:
                print "sending sudo password..."
                stdin.write('%s\n' % self.password()); stdin.flush()
            except:
                pass                 # could have closed in the meantime if password cached
        if not wait:
            return {'stdout':None, 'stderr':None, 'exit_status':None, 'note':"wait=False: '%s'"%cmd}
        while not stdout.channel.closed:
            time.sleep(0.05)
            if time.time() - start >= timeout:
                raise RuntimeError("on %s@%s command '%s' timed out"%(self._username, hostname, command))
        return {'stdout':stdout.read(), 'stderr':stderr.read(), 'exit_status':chan.recv_exit_status()}

    def public_ssh_keys(self, hostname, timeout=5):
        return '\n'.join([x['stdout'] for x in self.exec_command(hostname, 'cat .ssh/id_rsa.pub', timeout=timeout).values()])

    def git_pull(self, hostname, repo=GIT_REPO, timeout=30):
        return self(hostname, 'cd salvus && git pull %s'%repo, timeout=timeout)

    def build(self, hostname, pkg_name, timeout=250):
        return self(hostname, 'cd $HOME/salvus/salvus && . ./salvus-env && ./build.py --build_%s'%pkg_name, timeout=timeout)

    def python_c(self, hostname, cmd, timeout=30, sudo=False, wait=True):
        command = 'cd \"$HOME/salvus/salvus\" && . ./salvus-env && python -c "%s"'%cmd
        log.info("python_c: %s", command)
        return self(hostname, command, sudo=sudo, timeout=timeout, wait=wait)

    def apt_upgrade(self, hostname):
        # some nodes (e.g., sage nodes) have a firewall that disables upgrading via apt,
        # so we temporarily disable it.
        try:
            return self(hostname,'ufw --force disable && apt-get update && apt-get -y upgrade', sudo=True, timeout=120)
            # very important to re-enable the firewall, no matter what!
        finally:
            self(hostname,'ufw --force enable', sudo=True, timeout=120)


    def apt_install(self, hostname, pkg):
        # EXAMPLE:   hosts.apt_install('cassandra', 'openjdk-7-jre')
        try:
            return self(hostname, 'ufw --force disable && apt-get -y --force-yes install %s'%pkg, sudo=True, timeout=120)
        finally:
            self(hostname,'ufw --force enable', sudo=True, timeout=120)


    def reboot(self, hostname):
        return self(hostname, 'reboot -h now', sudo=True, timeout=5)

    def ufw(self, hostname, commands):
        if self[hostname] == ['127.0.0.1']:
            print "Not enabling firewall on 127.0.0.1"
            return
        cmd = ' && '.join(['ufw disable'] +
                          ['ufw default allow incoming'] + ['ufw default allow outgoing'] + ['ufw --force reset']
                          + ['ufw ' + c for c in commands] +
                             (['ufw --force enable'] if commands else []))
        return self(hostname, cmd, sudo=True, timeout=10, wait=False)

    def nodetool(self, args='', hostname='cassandra', wait=False, timeout=120):
        for k, v in self(hostname, 'salvus/salvus/data/local/cassandra/bin/nodetool %s'%args, timeout=timeout, wait=wait).iteritems():
            print k
            print v.get('stdout','')

    def update_hub_repos(self, parallel=True, wait=False):
        return self('hub','cd salvus/salvus; git pull 10.1.1.3:salvus && ./make_coffee ', parallel=parallel, wait=wait)

    #########################################################
    # SFTP support
    #########################################################
    def put(self, hostname, local_filename, remote_filename=None, timeout=5):
        if remote_filename is None:
            remote_filename = local_filename
        for hostname in self[hostname]:
            sftp = self.ssh(hostname, timeout=timeout).open_sftp()
            log.info('put: %s --> %s:%s', local_filename, hostname, remote_filename)
            sftp.put(local_filename, remote_filename)

    def putdir(self, hostname, local_path, remote_containing_path='.', timeout=5):
        # recursively copy over the local_path directory tree so that it is contained
        # in remote_containing_path on the target
        for hostname in self[hostname]:
            sftp = self.ssh(hostname, timeout=timeout).open_sftp()
            self._mkdir(sftp, remote_containing_path)
            for dirpath, dirnames, filenames in os.walk(local_path):
                print dirpath, dirnames, filenames
                self._mkdir(sftp, os.path.join(remote_containing_path, dirpath))
                for name in filenames:
                    local = os.path.join(dirpath, name)
                    remote = os.path.join(remote_containing_path, dirpath, name)
                    log.info('put: %s --> %s:%s', local, hostname, remote)
                    sftp.put(local, remote)

    def get(self, hostname, remote_filename, local_filename=None, timeout=5):
        if local_filename is None:
            local_filename = remote_filename
        ssh = self.ssh(hostname, timeout=timeout)
        sftp = ssh.open_sftp()
        sftp.get(remote_filename, local_filename)
        # If I want to implement recursive get of directory: http://stackoverflow.com/questions/6674862/recursive-directory-download-with-paramiko

    def rmdir(self, hostname, path, timeout=10):
        # this is a very dangerous function!
        self(hostname, 'rm -rf "%s"'%path, timeout=timeout)

    def _mkdir(self, sftp, path, mode=0o40700):
        try:
            sftp.mkdir(path, mode)
        except IOError:
            from stat import S_ISDIR
            if not S_ISDIR(sftp.stat(path).st_mode):
                raise IOError("remote '%s' (on %s) exists and is not a path"%(path, hostname))


    def mkdir(self, hostname, path, timeout=10, mode=0o40700):  # default mode is restrictive=user only, on general principle.
        for hostname in self[hostname]:
            ssh = self.ssh(hostname, timeout=timeout)
            sftp = ssh.open_sftp()
            self._mkdir(sftp, path, mode)

    def unlink(self, hostname, filename, timeout=10):
        for hostname in self[hostname]:
            ssh = self.ssh(hostname, timeout=timeout)
            sftp = ssh.open_sftp()
            try:
                sftp.remove(filename)
            except:
                pass # file doesn't exist

class Monitor(object):
    def __init__(self, hosts):
        self._hosts = hosts

    def snap(self):
        """
        Return information about the current active snap repo status.
        """
        cmd = "cd /mnt/snap/snap0/bup/`cat /mnt/snap/snap0/bup/active`&&BUP_DIR=. /usr/bin/time -f '%e' bup ls master|wc -l && du -s . && du -s .. && df -h /mnt/snap"
        ans = []
        for k, v in self._hosts('snap', cmd, wait=True, parallel=True, verbose=False).iteritems():
            d = {'service':'snap', 'host':k[0], 'status':'up' if (not v.get('exit_status',1) and 'error' not in (v['stderr'] + v.get('stdout','error')).lower()) else 'down'}

            if d['status'] == 'up':
                d['ls_time_s'] = float(v['stderr'].split()[0])
                d['commits'] = int(v['stdout'].split()[0])
                d['active_GB'] = int(int(v['stdout'].split()[1])/10.**6)
                d['bup_GB'] = int(int(v['stdout'].split()[3])/10.**6)
                d['use%'] = int(v['stdout'].split()[16][:-1])
            else:
                # No active file means snap is up but just no active repo.
                if 'active: No such file or directory' in v['stderr']:
                    d['status'] = 'up'
            ans.append(d)

        w = [(-d.get('ls_time_s',100000), d) for d in ans]
        w.sort()
        return [y for x,y in w]

    def cassandra(self):
        """
        Return information about the cassandra nodes.
        """
        # Determine up/down status using a random node
        import random
        hosts = self._hosts['cassandra']
        v = self._hosts(random.choice(hosts), "cd salvus/salvus&& . salvus-env&& nodetool status", wait=True, verbose=False)
        r = v[v.keys()[0]]
        status = {}
        for z in [x for x in r['stdout'].splitlines() if '%' in x]:
            w = z.split()
            status[w[1]] = 'up' if w[0] == "UN" else 'down'
        ans = []
        for k, v in self._hosts('cassandra', 'df -h /mnt/cassandra', wait=True, parallel=True, verbose=False).iteritems():
            if v.get('exit_status',1) or 'stdout' not in v:
                ans.append({'service':'cassandra', 'host':k[0], 'status':'down'})
            else:
                ans.append({'service':'cassandra', 'host':k[0], 'use%':int(v['stdout'].splitlines()[1].split()[4][:-1]), 'status':status.get(k[0],'down')})

        w = [((d['status'],d.get('use%','')), d) for d in ans]
        w.sort()
        return [y for x,y in w]

    def compute(self):
        hosts = self._hosts['cassandra']
        ans = []
        for k, v in self._hosts('compute', 'nproc && uptime && df -h /mnt/home/ && free -g && ps -C node -o args=|grep "local_hub.js run" |wc -l', wait=True, parallel=True).iteritems():
            d = {'host':k[0], 'service':'compute'}
            m = v.get('stdout','').splitlines()
            if v.get('exit_status',1) != 0 or len(m) != 9:
                d['status'] = 'down'
            else:
                d['status'] = 'up'
                d['nproc']  = int(m[0])
                z = m[1].replace(',','').split()
                d['load1']  = float(z[-3]) / d['nproc']
                d['load5']  = float(z[-2]) / d['nproc']
                d['load15'] = float(z[-1]) / d['nproc']
                z = m[3].split()
                d['use_GB'] =  int(z[2][:-1]) if z[2][-1] == 'G' else 1
                d['use%']   = z[4][:-1]
                z = m[5].split()
                d['ram_used_GB'] = int(z[2])
                d['ram_free_GB'] = int(z[3])
                d['nprojects'] = int(m[8])
                ans.append(d)
        w = [(-d['load1'], d) for d in ans]
        w.sort()
        return [y for x,y in w]

    def load(self):
        """
        Return normalized load on *everything*, sorted by highest current load first.
        """
        ans = []
        for k, v in self._hosts('all', 'nproc && uptime', parallel=True, wait=True).iteritems():
            d = {'host':k[0]}
            m = v.get('stdout','').splitlines()
            if v.get('exit_status',1) != 0 or len(m) < 2:
                d['status'] = 'down'
            else:
                d['status'] = 'up'
                d['nproc'] = int(m[0])
                z = m[1].replace(',','').split()
                d['load1'] = float(z[-3])/d['nproc']
                d['load5'] = float(z[-2])/d['nproc']
                d['load15'] = float(z[-1])/d['nproc']
                ans.append(d)
        w = [(-d['load1'], d) for d in ans]
        w.sort()
        return [y for x,y in w]

    def dns(self, hosts='all', rounds=3):
        """
        Verify that DNS is working well on all machines.
        """
        cmd = '&&'.join(["host -v google.com > /dev/null && host -v trac.sagemath.org >/dev/null && host -v www.sagemath.org >/dev/null && host -v github.com >/dev/null"]*rounds) + "; echo $?"
        ans = []
        for k, v in self._hosts(hosts, cmd, parallel=True, wait=True, timeout=5+4*rounds).iteritems():
            d = {'host':k[0], 'service':'dns'}
            exit_code = v.get('stdout','').strip()
            if exit_code == '':
                exit_code = '1'
            if exit_code=='1' or v.get('exit_status',1) != 0:
                d['status'] = 'down'
                print k,v
            else:
                d['status'] = 'up'
            ans.append(d)
        w = [((d.get('status','down'),d['host']),d) for d in ans]
        w.sort()
        return [y for x,y in w]
        return ans

    def all(self):
        return {
            'timestamp' : time.time(),
            'dns'       : self.dns(),
            'load'      : self.load(),
            'snap'      : self.snap(),
            'cassandra' : self.cassandra(),
            'compute'   : self.compute()
        }

    def status(self, all=None, n=5):
        if all is None:
            all = self.all( )
        print "DNS"
        for x in all['dns'][:n]:
            print x

        print "LOAD"
        for x in all['load'][:n]:
            print x

        print "SNAP"
        for x in all['snap'][:n]:
            print x

        print "CASSANDRA"
        for x in all['cassandra'][:n]:
            print x

        print "COMPUTE"
        vcompute = all['compute']
        print "%s projects running"%(sum([x['nprojects'] for x in vcompute]))
        for x in all['compute'][:n]:
            print x

    def update_db(self, all=None):
        if all is None:
            all = self.all()
        import cassandra, json
        t = all['timestamp']
        d = {}
        for k, v in all.iteritems():
            d[k] = json.dumps(v, separators=(',',':'))

        d['day']       = time.strftime("%Y-%m-%d")
        d['hour']      = int(time.strftime("%H"))
        d['minute']    = int(time.strftime("%M"))
        d['timestamp'] = int(time.time())
        password = open(os.path.join(SECRETS, 'cassandra/monitor')).read().strip()
        cassandra.cursor_execute("UPDATE monitor SET timestamp=:timestamp, dns=:dns, load=:load, snap=:snap, cassandra=:cassandra, compute=:compute WHERE day=:day and hour=:hour and minute=:minute",  param_dict=d, user='monitor', password=password)
        cassandra.cursor_execute("UPDATE monitor_last SET timestamp=:timestamp, dns=:dns, load=:load, snap=:snap, cassandra=:cassandra, compute=:compute, day=:day, hour=:hour, minute=:minute WHERE dummy=true",  param_dict=d, user='monitor', password=password)

    def go(self, wait=61):
        import time
        while True:
            all = self.all()
            self.update_db(all=all)
            self.status(all=all)
            time.sleep(wait)

class Services(object):
    def __init__(self, path, username=whoami, keyspace='salvus', passwd=True):
        """
        - passwd -- if False, don't ask for a password; in this case nothing must require sudo to
          run, and all logins must work using ssh with keys
        """
        self._keyspace = keyspace
        self._path = path
        self._username = username
        self._hosts = Hosts(os.path.join(path, 'hosts'), username=username, passwd=passwd)

        self._services, self._ordered_service_names = parse_groupfile(os.path.join(path, 'services'))
        del self._services[None]

        self.monitor = Monitor(Hosts(os.path.join(path, 'hosts'), username=username, passwd=False))

        # this is the canonical list of options, expanded out by service and host.
        def hostopts(service, query='all', copy=True):
            """Return list of pairs (hostname, options) defined in the services file, where
            the hostname matches the given hostname/group"""
            restrict = set(self._hosts[query])
            return sum([[(h, dict(opts) if copy else opts) for h in self._hosts[query] if h in restrict]
                               for query, opts in self._services[service]], [])

        self._options = dict([(service, hostopts(service)) for service in self._ordered_service_names])

        ##########################################
        # Programatically fill in extra options to the list
        ##########################################
        # CASSANDRA options
        if 'cassandra' in self._options:
            v = self._options['cassandra']
            # determine the seeds
            seeds = ','.join([h for h, o in v if o.get('seed',False)])
            # determine global topology file; ip_address=data_center:rack
            topology = '\n'.join(['%s=%s'%(h, o.get('topology', 'DC0:RAC0'))
                                                                  for h, o in v] + ['default=DC0:RAC0'])
            for address, o in v:
                o['seeds'] = seeds
                o['topology'] = topology
                o['listen_address'] = address
                o['rpc_address'] = address
                if 'seed' in o: del o['seed']

            native_transport_port = v[0][1].get('native_transport_port', 9042)
            if native_transport_port != 9042:
                print "Serving cassandra on non-standard port %s"%native_transport_port
            try:
                self._cassandra = ['%s:%s'%(h, native_transport_port) for h in self._hosts['cassandra']]
                import cassandra
                cassandra.KEYSPACE = self._keyspace
                cassandra.set_nodes(self._hosts['cassandra'])
            except ValueError:
                print "WARNING: no cassandra hosts -- severly degraded functionality!"

        # HAPROXY options
        if 'haproxy' in self._options:
            nginx_servers = [{'ip':h,'port':o.get('port',NGINX_PORT), 'maxconn':10000}
                             for h, o in self._options['nginx']]
            hub_servers = [{'ip':h,'port':o.get('port',HUB_PORT), 'maxconn':100}
                              for h, o in self._options['hub']]
            # NOTE: right now we assume that the proxy servers are running on exactly the same machine as the hub,
            # since they are implemented as part of the same process (though, listening on a different ports).
            proxy_servers = [{'ip':h,'proxy_port':o.get('proxy_port',HUB_PROXY_PORT), 'maxconn':100}
                              for h, o in self._options['hub']]
            for _, o in self._options['haproxy']:
                if 'nginx_servers' not in o:
                    o['nginx_servers'] = nginx_servers
                if 'hub_servers' not in o:
                    o['hub_servers'] = hub_servers
                if 'proxy_servers' not in o:
                    o['proxy_servers'] = proxy_servers

        # HUB options
        if 'hub' in self._options:
            for host, o in self._options['hub']:
                # very important: set to listen only on our VPN.
                o['host'] = host

        # SNAP options
        if 'snap' in self._options:
            for host, o in self._options['snap']:
                # very important: set to listen only on our VPN.
                o['host'] = host

        # COMPUTE options
        if 'compute' in self._options:
            for host, o in self._options['compute']:
                # Very, very important: set to listen only on our VPN!
                # There is rumored to be an attack where a local user
                # can bind to a more specific host and same port on a
                # machine, and intercept all trafic.  This would mean
                # they could effectively man-in-the-middle take over a
                # node.  By binding on a specific ip address, we
                # prevent this possibility.  I haven't tried this, so
                # I'm not 100% sure.
                o['host'] = host

        if 'sagenb' in self._options:
            for address, o in self._options['sagenb']:
                o['address'] = address

        # VM options
        if 'vm' in self._options:
            for address, o in self._options['vm']:
                # very, very important: set to listen only on our VPN!  There is an attack where a local user
                # can bind to a more specific address and same port on a machine, and intercept all trafic.
                if 'ip_address' not in o:
                    addresses = self._hosts[o['hostname']]
                    if len(addresses) != 1:
                        raise RuntimeError("Error configuring a VM: hostname %s doesn't uniquely determine one ip address"%o['hostname'])
                    o['ip_address'] = addresses[0]


    def _hostopts(self, service, hostname, opts):
        """
        Return copy of pairs (hostname, options_dict) for the given
        service, restricted by the given hostname.
        """
        hosts = set(self._hosts[hostname])
        opts1 = set(opts.iteritems())
        return [(h,dict(o)) for h,o in self._options[service] if h in hosts and opts1.issubset(set([(x,y) for x, y in o.iteritems() if x in opts]))]

    def _do_action(self, name, action, address, options, db_string, wait):

        if 'sudo' in options:
            sudo = True
            del options['sudo']
        else:
            sudo = False
        if 'timeout' in options:
            timeout = options['timeout']
            del options['timeout']
        else:
            timeout = 30


        if 'id' not in options:
            options['id'] = 0

        cmd = "import admin; print admin.%s(%s, **%r).%s()"%(name, db_string, options, action)

        if name == "Cassandra":
            self.cassandra_firewall(address, action)

        elif name == "Stunnel":
            self.stunnel_key_files(address, action)

        ret = self._hosts.python_c(address, cmd, sudo=sudo, timeout=timeout, wait=wait)

        if name == "Compute":
            # TODO: put in separate function
            log.info("Starting compute firewall")
            self.compute_firewall(address, action)
            log.info("Recording compute server in Cassandra")
            import cassandra
            if action in ['start', 'restart']:
                log.info("Recording compute server START in Cassandra")
                try:
                    cassandra.record_that_compute_server_started(address)
                except RuntimeError, msg:
                    print msg
            elif action == 'stop':
                log.info("Recording compute server STOP in Cassandra")
                try:
                    cassandra.record_that_compute_server_stopped(address)
                except RuntimeError, msg:
                    print msg

        return (address, self._hosts.hostname(address), options, ret)

    def _action(self, service, action, host, opts, wait, parallel):
        if service not in self._services:
            raise ValueError("unknown service '%s'"%service)


        name = service.capitalize()
        db_string = "" if (name=='Compute' or not hasattr(self, '_cassandra')) else "monitor_database='%s'"%(','.join(self._cassandra))
        v = self._hostopts(service, host, opts)

        self._hosts.password()  # can't get password in thread

        w = [((name, action, address, options, db_string, wait),{}) for address, options in v]

        if parallel:
            return misc.thread_map(self._do_action, w)
        else:
            return [self._do_action(*args, **kwds) for args, kwds in w]

    def stunnel_key_files(self, hostname, action):
        target = os.path.join(BASE, SECRETS)
        for ip_address in self._hosts[hostname]:
            if ip_address.startswith('127.'): continue  # don't wipe our personal copy of secrets!
            if action == 'stop':
                self._hosts.rmdir(ip_address, os.path.join(target, 'sagemath'))
            elif action in ['start', 'restart']:
                self._hosts.mkdir(ip_address, target)
                self._hosts.putdir(ip_address, os.path.join(SECRETS, 'sagemath'), BASE)
        # avoid race condition where file is there but not there.
        time.sleep(.5)

    def cassandra_firewall(self, hostname, action):
        if action == "restart":
            action = 'start'
        if action == "stop":
            commands = []
        elif action == "start":
            # hub hosts can connect to CASSANDRA_CLIENT_PORT and CASSANDRA_NATIVE_PORT
            # cassandra hosts can connect to CASSANDRA_INTERNODE_PORTS
            commands = (['allow proto tcp from %s to any port %s'%(host, CASSANDRA_CLIENT_PORT) for host in self._hosts['hub admin snap']] +
                        ['allow proto tcp from %s to any port %s'%(host, CASSANDRA_NATIVE_PORT) for host in self._hosts['hub admin snap']] +
                        ['allow proto tcp from %s to any port %s'%(host, port)
                                for host in self._hosts['cassandra admin'] for port in CASSANDRA_INTERNODE_PORTS] +
                        ['deny proto tcp from any to any port %s'%(','.join([str(x) for x in CASSANDRA_PORTS]))])
        elif action == 'status':
            return
        else:
            raise ValueError("unknown action '%s'"%action)
        return self._hosts.ufw(hostname, commands)

    def compute_firewall(self, hostname, action):
        if action == "restart":
            action = 'start'
        if action == "stop":
            commands = []
        elif action == "start":   # 22=ssh, 53=dns, 655=tinc vpn,

            # Use iptables directly -- *not* ufw -- to drop incoming traffic within compute machines!  This is critical to make
            # servers-with-passwords that users start listening on the vpn safe, such as ipython.
            cmd = 'iptables -I INPUT --src %s -p tcp --dport 1025:65535 -j DROP'%(','.join(self._hosts.ip_addresses('compute')))
            print cmd
            self._hosts(hostname, cmd, sudo=True, timeout=10, wait=False)


            # Use iptables directly -- *not* ufw -- to drop incoming traffic within compute machines!  This is critical to make
            # servers-with-passwords that users start listening on the vpn safe, such as ipython.
            cmd = 'iptables -I INPUT --src %s -p tcp --dport 1025:65535 -j DROP'%(','.join(self._hosts.ip_addresses('compute')))
            print cmd
            self._hosts(hostname, cmd, sudo=True, timeout=10, wait=False)

            commands = (['allow proto tcp from %s to any port 1:65535'%ip for ip in self._hosts['hub admin']] +  # allow access from hub/admin
                        ['allow proto udp from %s to any port 1:65535'%ip for ip in self._hosts['hub admin']] +
                        ['deny proto tcp to any port 1025:65535'] +          # deny access to user ports (except from hub) - CRITICAL so users
                        ['deny proto udp to any port 1025:65535'])           # can safely open a server on localhost

            # This would firewall the compute machines in various additional ways, which we've decided not to do for now.  E.g., this in various additional ways, which we've decided not to do for now.  E.g., this
            # makes things like github, etc., not work with cloud, which is not worth restricting.
            # makes things like github, etc., not work with cloud, which is not worth restricting.
            #commands = (['default deny outgoing'] + ['allow %s'%p for p in [22,655]] + ['allow out %s'%p for p in [22,53,655]] +
            #            ['allow proto tcp from %s to any port %s'%(ip, y[1]) for ip in self._hosts['hub admin'] for y in COMPUTE_SERVER_PORTS.iteritems()]+
            #            ['deny proto tcp to any port 1:65535', 'deny proto udp to any port 1:65535']
            #            )
        elif action == 'status':
            return
        else:
            raise ValueError("unknown action '%s'"%action)

        return self._hosts.ufw(hostname, commands)

    def _all(self, callable, reverse=False):
        names = self._ordered_service_names
        return dict([(s, callable(s)) for s in (reversed(names) if reverse else names)])

    def start(self, service, host='all', wait=True, parallel=False, **opts):
        if service == 'all':
            return self._all(lambda x: self.start(x, host=host, wait=wait, **opts), reverse=False)
        return self._action(service, 'start', host, opts, wait=wait, parallel=parallel)

    def stop(self, service, host='all', wait=True, parallel=False, **opts):
        if service == 'all':
            return self._all(lambda x: self.stop(x, host=host, wait=wait, **opts), reverse=True)
        return self._action(service, 'stop', host, opts, wait, parallel=parallel)

    def status(self, service, host='all', wait=True, parallel=False, **opts):
        if service == 'all':
            return self._all(lambda x: self.status(x, host=host, wait=True, **opts), reverse=False)
        return self._action(service, 'status', host, opts, wait=True, parallel=parallel)

    def restart(self, service, host='all', wait=True, reverse=True, parallel=False, **opts):
        if service == 'all':
            return self._all(lambda x: self.restart(x, host=host, reverse=reverse, wait=wait, **opts), reverse=reverse)
        return self._action(service, 'restart', host, opts, wait, parallel=parallel)

    def wait_until_up(self, host='all'):
        while True:
            v = self._hosts.ping(host)[1]
            if not v: return
            log.info("Waiting for %s"%(v,))

    def start_system(self):
        log.info(" ** Waiting for kvm hosts")
        if self._hosts.is_valid_hostname('kvm-host'):
            self.wait_until_up('kvm-host')
        log.info(" ** Starting virtual machines")
        if 'vm' in self._services:
            self.start('vm', parallel=True, wait=False)
        log.info(" ** Waiting for VM's to all finish starting")
        self.wait_until_up('all')
        log.info(" ** Setting up compute VM's firewall")
        self.compute_firewall('compute', 'start')
        log.info(" ** Starting cassandra databases.")
        self.start('cassandra', wait=True, parallel=True)
        for service in ['haproxy', 'nginx', 'hub', 'snap']:
            log.info(" ** Starting %s", service)
            self.start(service, parallel=True, wait=False)
        #log.info(" ** Starting compute")
        #self.start('compute', parallel=False, wait=False)
        #self.monitor_hubs()

    def stop_system(self):
        if 'cassandra' in self._services:
            self.stop('cassandra', parallel=True, wait=True)
        for service in ['haproxy', 'nginx', 'hub', 'snap']:
            self.stop(service, parallel=True, wait=True)
        if 'vm' in self._services:
            self.stop('vm', parallel=True)
        while True:
            time.sleep(1)
            # TODO: this is horrible
            if 'vm' in self._services:
                v = [X[1] for X in self.status('vm',parallel=True) if 'cputime' in X[3].items()[0][1]['stdout']]
                if v:
                    print "Waiting to terminate: %s"%(', '.join(v))
                else:
                    break
        print "All vm's successfully terminated"

    def restart_web(self):
        """
        Restart everything related to the web nodes including VM's.
        This is everything sitting between the clients and the compute nodes.
        Call this when doing upgrades that don't modify
        the Sage install or python client code on the compute machines.  This is minimally
        disruptive to users, at least compared to a full restart of compute machines!
        """
        services = 'hub nginx snap'
        for service in services.split():
            self.stop(service, wait=True, parallel=True)
        web_hosts = [x for x in self._hosts._canonical_hostnames.values() if 'web' in x]
        for hostname in web_hosts:
            self.restart('vm',hostname=hostname, parallel=True, wait=False)
        self.wait_until_up(' '.join(web_hosts))
        for service in services.split():
            self.start(service, parallel=True, wait=True)

    def gentle_restart(self, services='stunnel haproxy web compute cassandra'):
        """
          - (~1 minute) restart each stunnel, one at a time.
          - (~5 seconds) restart each haproxy, one at a time.
          - (~20 minutes = 1 minute per vm) for each web machine, one at time.
               - stop snap
               - stop nginx
               - stop hub
               - restart the vm
               - start hub
               - start nginx
               - start snap
       (*)  - broadcast message to all clients "system maintenance -- The project servers are restarting and will be unavailable for up to 1 minute."...
         - (~1 minute) restart all compute machine simultaneously (if projects moved then we would do differently)
         - for each cassandra machine, one a time:
               - stop cassandra
               - restart the vm
               - start cassandra
        """
        if isinstance(services, str):
            services = services.split()
        services = set(services)
        t = time.time()
        if 'stunnel' in services:
            print "Restarting STUNNEL"
            self.restart('stunnel')

        print "time: ", time.time()-t
        if 'haproxy' in services:
            print "Restarting HAPROXY"
            self.restart('haproxy')

        print "time: ", time.time()-t

        if 'web' in services:
            print "Restarting web server hosts"
            # We are assuming that snap/nginx/hub are all on the same VM's.
            v = self._hosts['snap']
            for i, host in enumerate(v):
                print "*"*70
                print "web HOST (%s of %s): %s"%(i+1, len(v), host)
                print "*"*70
                self.stop('snap', host=host, wait=True)
                self.stop('nginx', host=host, wait=True)
                self.stop('hub', host=host, wait=True)
                print "resting vm..."
                self.restart('vm',ip_address=host, wait=True)
                print "WAITING FOR -- web HOST (%s of %s): %s"%(i, len(v), host)
                self.wait_until_up(host)
                self.start('hub', host=host, wait=False)
                self.start('nginx', host=host, wait=False)
                self.start('snap', host=host, wait=False)
                print "time: ", time.time()-t

        if 'compute' in services:
            print "Restarting compute VM's"
            print [self.restart('vm', ip_address=host, wait=False) for host in self._hosts['compute']]
            self.wait_until_up('compute')
            print "time: ", time.time()-t

        if 'cassandra' in services:
            print "Restarting cassandra hosts"
            v = self._hosts['cassandra']
            for i, host in enumerate(v):
                print "*"*70
                print "cassandra HOST (%s of %s): %s"%(i+1, len(v), host)
                print "*"*70
                self.stop('cassandra', host=host, wait=True)
                self.restart('vm', ip_address=host, wait=True)
                self.wait_until_up(host)
                print "WAITING FOR -- cassandra HOST (%s of %s): %s"%(i, len(v), host)
                self.start('cassandra', host=host, wait=False)
                print "time: ", time.time()-t

    def update_nginx_from_dev_repo(self):
        """
        Pull from the devel repo on all web machines and update coffeescript, etc., but do not
        update version number.  Also, restart nginx.  Use this for pushing out HTML/Javascript/CSS
        changes that aren't at all critical for users to see immediately.
        """
        self._hosts('hub', 'cd salvus/salvus; . salvus-env; sleep $(($RANDOM%5)); ./pull_from_dev_project; ./make_coffee --all', parallel=True, timeout=30)
        self.restart('nginx')

    def update_web_servers_from_dev_repo(self):
        """
        Pull from the devel repo on all web machines, update version uniformly
        across all machines, then restart all nginx and hub servers, in serial.
        """
        import time; ver = int(time.time())
        self._hosts('hub', 'cd salvus/salvus; . salvus-env; sleep $(($RANDOM%5)); ./pull_from_dev_project; echo "exports.version=%s" > node_modules/salvus_version.js; ./make_coffee --all'%ver, parallel=True, timeout=30)
        self.restart('nginx')
        self.restart('hub')




