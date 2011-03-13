#!c:\\python\\python.exe

import subprocess
import threading
import getopt
import time
import sys
import os

from sulley import pedrpc

sys.path.append(r"..\..\paimei")

from pydbg         import *
from pydbg.defines import *

import utils

PORT  = 26002
ERR   = lambda msg: sys.stderr.write("ERR> " + msg + "\n") or sys.exit(1)
USAGE = "USAGE: process_monitor.py"                                                                \
        "\n    <-c|--crash_bin FILENAME> filename to serialize crash bin class to"                 \
        "\n    [-p|--proc_name NAME]     process name to search for and attach to"                 \
        "\n    [-i|--ignore_pid PID]     ignore this PID when searching for the target process"    \
        "\n    [-l|--log_level LEVEL]    log level (default 1), increase for more verbosity"       \
        "\n    [--port PORT]             TCP port to bind this agent to"


########################################################################################################################
class debugger_thread (threading.Thread):
    def __init__ (self, process_monitor, proc_name, ignore_pid=None):
        '''
        Instantiate a new PyDbg instance and register user and access violation callbacks.
        '''

        threading.Thread.__init__(self)

        self.process_monitor  = process_monitor
        self.proc_name        = proc_name
        self.ignore_pid       = ignore_pid

        self.access_violation = False
        self.active           = True
        self.dbg              = pydbg()
        self.pid              = None

        # give this thread a unique name.
        self.setName("%d" % time.time())

        self.process_monitor.log("debugger thread initialized with UID: %s" % self.getName(), 5)

        # set the user callback which is response for checking if this thread has been killed.
        self.dbg.set_callback(USER_CALLBACK_DEBUG_EVENT,  self.dbg_callback_user)
        self.dbg.set_callback(EXCEPTION_ACCESS_VIOLATION, self.dbg_callback_access_violation)


    def dbg_callback_access_violation (self, dbg):
        '''
        Ignore first chance exceptions. Record all unhandled exceptions to the process monitor crash bin and kill
        the target process.
        '''

        # ignore first chance exceptions.
        if dbg.dbg.u.Exception.dwFirstChance:
            return DBG_EXCEPTION_NOT_HANDLED

        # raise the access violaton flag.
        self.access_violation = True

        # record the crash to the process monitor crash bin.
        # include the test case number in the "extra" information block.
        self.process_monitor.crash_bin.record_crash(dbg, self.process_monitor.test_number)

        # save the the crash synopsis.
        self.process_monitor.last_synopsis = self.process_monitor.crash_bin.crash_synopsis()
        first_line                         = self.process_monitor.last_synopsis.split("\n")[0]

        self.process_monitor.log("debugger thread-%s caught access violation: '%s'" % (self.getName(), first_line))

        # this instance of pydbg should no longer be accessed, i want to know if it is.
        self.process_monitor.crash_bin.pydbg = None

        # kill the process.
        dbg.terminate_process()
        return DBG_CONTINUE


    def dbg_callback_user (self, dbg):
        '''
        The user callback is run roughly every 100 milliseconds (WaitForDebugEvent() timeout from pydbg_core.py). Simply
        check if the active flag was lowered and if so detach from the target process. The thread should then exit.
        '''

        if not self.active:
            self.process_monitor.log("debugger thread-%s detaching" % self.getName(), 5)
            dbg.detach()

        return DBG_CONTINUE


    def run (self):
        '''
        Main thread routine, called on thread.start(). Thread exits when this routine returns.
        '''

        self.process_monitor.log("debugger thread-%s looking for process name: %s" % (self.getName(), self.proc_name))

        # watch for and try attaching to the process.
        try:
            self.watch()
            self.dbg.attach(self.pid)
            self.dbg.run()
            self.process_monitor.log("debugger thread-%s exiting" % self.getName())
        except:
            pass

        # XXX - removing the following line appears to cause some concurrency issues.
        del(self.dbg)


    def watch (self):
        '''
        Continuously loop, watching for the target process. This routine "blocks" until the target process is found.
        Update self.pid when found and return.
        '''

        while not self.pid:
            for (pid, name) in self.dbg.enumerate_processes():
                # ignore the optionally specified PID.
                if pid == self.ignore_pid:
                    continue

                if name.lower() == self.proc_name.lower():
                    self.pid = pid
                    break

        self.process_monitor.log("debugger thread-%s found match on pid %d" % (self.getName(), self.pid))


########################################################################################################################
class process_monitor_pedrpc_server (pedrpc.server):
    def __init__ (self, host, port, crash_filename, proc_name=None, ignore_pid=None, log_level=1):
        '''
        @type  host:           String
        @param host:           Hostname or IP address
        @type  port:           Integer
        @param port:           Port to bind server to
        @type  crash_filename: String
        @param crash_filename: Name of file to (un)serialize crash bin to/from
        @type  proc_name:      String
        @param proc_name:      (Optional, def=None) Process name to search for and attach to
        @type  ignore_pid:     Integer
        @param ignore_pid:     (Optional, def=None) Ignore this PID when searching for the target process
        @type  log_level:      Integer
        @param log_level:      (Optional, def=1) Log output level, increase for more verbosity
        '''

        # initialize the PED-RPC server.
        pedrpc.server.__init__(self, host, port)

        self.crash_filename   = crash_filename
        self.proc_name        = proc_name
        self.ignore_pid       = ignore_pid
        self.log_level        = log_level

        self.stop_commands    = []
        self.start_commands   = []
        self.test_number      = None
        self.debugger_thread  = None
        self.crash_bin        = utils.crash_binning.crash_binning()
            
        self.last_synopsis    = ""

        if not os.access(os.path.dirname(self.crash_filename), os.X_OK):
            self.log("invalid path specified for crash bin: %s" % self.crash_filename)
            raise Exception

        # restore any previously recorded crashes.
        try:
            self.crash_bin.import_file(self.crash_filename)
        except:
            pass

        self.log("Process Monitor PED-RPC server initialized:")
        self.log("\t crash file:  %s" % self.crash_filename)
        self.log("\t # records:   %d" % len(self.crash_bin.bins))
        self.log("\t proc name:   %s" % self.proc_name)
        self.log("\t log level:   %d" % self.log_level)
        self.log("awaiting requests...")


    def alive (self):
        '''
        Returns True. Useful for PED-RPC clients who want to see if the PED-RPC connection is still alive.
        '''

        return True


    def get_crash_synopsis (self):
        '''
        Return the last recorded crash synopsis.

        @rtype:  String
        @return: Synopsis of last recorded crash.
        '''

        return self.last_synopsis


    def get_bin_keys (self):
        '''
        Return the crash bin keys, ie: the unique list of exception addresses.

        @rtype:  List
        @return: List of crash bin exception addresses (keys).
        '''

        return self.crash_bin.bins.keys()


    def get_bin (self, bin):
        '''
        Return the crash entries from the specified bin or False if the bin key is invalid.

        @type  bin: Integer (DWORD)
        @param bin: Crash bin key (ie: exception address)

        @rtype:  List
        @return: List of crashes in specified bin.
        '''

        if not self.crash_bin.bins.has_key(bin):
            return False

        return self.crash_bin.bins[bin]


    def log (self, msg="", level=1):
        '''
        If the supplied message falls under the current log level, print the specified message to screen.

        @type  msg: String
        @param msg: Message to log
        '''

        if self.log_level >= level:
            print "[%s] %s" % (time.strftime("%I:%M.%S"), msg)


    def post_send (self):
        '''
        This routine is called after the fuzzer transmits a test case and returns the status of the target.

        @rtype:  Boolean
        @return: Return True if the target is still active, False otherwise.
        '''

        av = self.debugger_thread.access_violation

        # if there was an access violation, wait for the debugger thread to finish then kill thread handle.
        # it is important to wait for the debugger thread to finish because it could be taking its sweet ass time
        # uncovering the details of the access violation.
        if av:
            while self.debugger_thread.isAlive():
                time.sleep(1)

            self.debugger_thread = None

        # serialize the crash bin to disk.
        self.crash_bin.export_file(self.crash_filename)

        bins    = len(self.crash_bin.bins)
        crashes = 0

        for bin in self.crash_bin.bins.keys():
            crashes += len(self.crash_bin.bins[bin])

        return not av


    def pre_send (self, test_number):
        '''
        This routine is called before the fuzzer transmits a test case and ensure the debugger thread is operational.

        @type  test_number: Integer
        @param test_number: Test number to retrieve PCAP for.
        '''

        self.log("pre_send(%d)" % test_number, 10)
        self.test_number = test_number

        # unserialize the crash bin from disk. this ensures we have the latest copy (ie: vmware image is cycling).
        try:
            self.crash_bin.import_file(self.crash_filename)
        except:
            pass

        # if we don't already have a debugger thread, instantiate and start one now.
        if not self.debugger_thread or not self.debugger_thread.isAlive():
            self.log("creating debugger thread", 5)
            self.debugger_thread = debugger_thread(self, self.proc_name, self.ignore_pid)
            self.debugger_thread.start()
            self.log("giving debugger thread 2 seconds to settle in", 5)
            time.sleep(2)


    def start_target (self):
        '''
        Start up the target process by issuing the commands in self.start_commands.
        '''

        self.log("starting target process")

        for command in self.start_commands:
            subprocess.Popen(command)

        self.log("done. target up and running, giving it 5 seconds to settle in.")
        time.sleep(5)


    def stop_target (self):
        '''
        Kill the current debugger thread and stop the target process by issuing the commands in self.stop_commands.
        '''

        # give the debugger thread a chance to exit.
        time.sleep(1)

        self.log("stopping target process")

        for command in self.stop_commands:
            if command == "TERMINATE_PID":
                dbg = pydbg()
                for (pid, name) in dbg.enumerate_processes():
                    if name.lower() == self.proc_name.lower():
                        os.system("taskkill /pid %d" % pid)
                        break
            else:
                os.system(command)


    def set_proc_name (self, proc_name):
        self.log("updating target process name to '%s'" % proc_name)

        self.proc_name = proc_name


    def set_start_commands (self, start_commands):
        self.log("updating start commands to: %s" % start_commands)

        self.start_commands = start_commands


    def set_stop_commands (self, stop_commands):
        self.log("updating stop commands to: %s" % stop_commands)

        self.stop_commands = stop_commands


########################################################################################################################

if __name__ == "__main__":
    # parse command line options.
    try:
        opts, args = getopt.getopt(sys.argv[1:], "c:i:l:p:", ["crash_bin=", "ignore_pid=", "log_level=", "proc_name=", "port="])
    except getopt.GetoptError:
        ERR(USAGE)

    crash_bin = ignore_pid = proc_name = None
    log_level = 1

    for opt, arg in opts:
        if opt in ("-c", "--crash_bin"):   crash_bin  = arg
        if opt in ("-i", "--ignore_pid"):  ignore_pid = int(arg)
        if opt in ("-l", "--log_level"):   log_level  = int(arg)
        if opt in ("-p", "--proc_Name"):   proc_name  = arg
        if opt in ("-P", "--port"):        PORT       = int(arg)

    if not crash_bin:
        ERR(USAGE)

    # spawn the PED-RPC servlet.
    try:
        servlet = process_monitor_pedrpc_server("0.0.0.0", PORT, crash_bin, proc_name, ignore_pid, log_level)
        servlet.serve_forever()
    except:
        print "Error starting RPC server!"
        pass