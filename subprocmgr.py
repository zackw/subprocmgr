# subprocmgr - Subprocess manager (Python component)
#
# Copyright © 2015 Zack Weinberg
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# There is NO WARRANTY.

"""This is a semi-compatible replacement for the `subprocess` module.
   Together with its C helper program, it solves a headache when writing a
   Python program that runs a bunch of subprocesses and consumes their
   output: There is no way to select() for the termination of a specific
   process, and therefore no reliable way to know when to call wait().

   The most significant divergence from the `subprocess` API is that
   `Popen` objects do not provide the `poll`, `wait`, or `communicate`
   methods, nor the `stdin`, `stdout`, or `stderr` properties.
   Instead, there is a `status` property, which is a `queue.Queue`;
   data on the queue consists of `SubprocessStatus` objects, which
   carry all of the information that you could have gotten from the
   above methods, but with an ordering guarantee.

   The special constants available for the `stdin`, `stdout`, and
   `stderr` arguments to the `Popen` constructor are different, and
   the default behavior (if these arguments are not specified) is also
   different:

       subprocmgr.DEVNULL - Use /dev/null.
                            This is the default for `stdin`.
       subprocmgr.RECEIVE - Receive output via `status`.
                            This is the default for `stdout` and `stderr`.
       subprocmgr.INHERIT - Inherit this file descriptor.  It will point to
                            whatever it pointed to in the parent process
                            *at the time the C helper program was started*.

   If you want to set a standard stream to a pipe, you have to create
   the pipe manually.  It is strongly recommended that you only do this
   for intermediate channels in a pipeline.

   The `bufsize`, `shell`, `preexec_fn`, `pass_fds`, `close_fds`,
   `restore_signals`, and `start_new_session` arguments to the `Popen`
   constructor are not implemented.  Some of these are implementable
   if needed, others are not.  (The behavior is as-if `close_fds=True`
   and `restore_signals=True`.)

   The new constructor arguments `status_queue` and
   `package_status_message` allow you to supply an existing queue that
   should receive status messages, and package `SubprocessStatus`
   objects if it is necessary to make them fit the queue's format
   (e.g. if it's a priority queue).

   The convenience functions `call`, `check_call`, and `check_output`
   are not implemented.  They wouldn't be hard to add, but if you need
   this module you probably aren't using them anyway.

   The module function `start_manager` can be used to start up the C
   helper program and internal event loop, if this needs to happen
   before the first call to `subprocmgr.Popen` for some reason (e.g.
   if you want to do it before creating threads, or if you are going
   to change what stdin/stdout/stderr point to).  `stop_manager` stops
   them again; this also automatically happens when the program exits.
   Note that invoking `stop_manager` causes all subprocesses that are
   still running to receive a SIGTERM, and five seconds later, any
   survivors receive a SIGKILL; it will not return until all
   outstanding subprocesses have exited.  (Their final output is
   posted to the respective .status queues as usual.)

   Finally, note that the *direct* parent of processes created using this
   module is the C helper program, which itself runs as a subprocess of
   your program.  This could cause problems in edge cases.

   This module currently only works on Linux, because the C helper program
   currently uses Linux-specific APIs (namely, epoll(2) and signalfd(2)).
   Patches to make it work on operating systems which implement kqueue(2)
   will cheerfully be accepted.  Patches for portability to other Unixes
   may or may not be accepted, depending on how horrible the necessary
   contortions are.

   There is probably no hope of making this module work on Windows, but if
   someone is prepared to do all of the work and all of the subsequent
   maintenance, I would at least consider the proposal.  (You will
   *certainly* need to reimplement the helper program from scratch, and
   possibly also the communication channel to this module.)

"""

import atexit
import errno
import fcntl
import logging
import os
import queue
import resource
import socket
import struct
import threading

#
# Public constants
#

# These must not collide with the legitimate fd space (nonnegative
# integers) or with file objects.  We also don't want them to collide
# with the similar constants from the subprocess module.
RECEIVE = "RECEIVE"
INHERIT = "INHERIT"
DEVNULL = "DEVNULL"

#
# Internal utilities
#

_log = logging.getLogger(__name__)
_helper_path = os.path.join(os.path.dirname(__file__), "subprocmgr")

def interpret_wait_status(status):
    """Internal: produce a human-readable message from a wait status."""
    if os.WIFEXITED(status):
        if os.WEXITSTATUS(status) == 0:
            return "exited normally"
        else:
            return "exited with failure code {}".format(os.WEXITSTATUS(status))
    elif os.WIFSIGNALED(status):
        return "killed by signal {}".format(os.WTERMSIG(status))
    else:
        return "posted uninterpretable wait status {:04x}".format(status)

def start_helper_process(control_socket):
    """Internal: start up a helper process connected to 'control_socket',
       and return its PID."""

    pid = os.fork()
    if pid: return pid

    # We are the child.  We want to inherit fds 0, 1, and 2 as is,
    # place the control socket at fd 3, and close all higher-numbered
    # file descriptors.  If this is 3.4, we can rely on the stdlib's
    # general policy of non-inheritance (with the clever exception of
    # the result of a call to dup2()!) to do that for us; otherwise we
    # have to close higher-numbered fds manually, and neither Linux
    # nor Python gives us closefrom(), grumble.
    successful_dup2 = False
    try:
        os.dup2(control_socket.fileno(), 3)
        successful_dup2 = True

        if not hasattr(os, 'set_inheritable'):
            # This is the least terrible way to simulate closefrom()
            # on Linux that I am aware of.  There doesn't seem to be
            # any way to find out the number of the fd used to scan
            # /proc/self/fd (which _will_ be in the list of fds
            # returned) so we just close it too and ignore the error.
            # Creating a list in advance ensures that we won't close
            # that fd out from under os.listdir (which I believe is
            # _not_ a generator in any released version of Python,
            # but that could easily change).
            try:
                fds = [x for x in
                       (int(xx) for xx in os.listdir("/proc/self/fd"))
                       if x >= 4]
            except OSError:
                s, h = resource.getrlimit(resource.RLIMIT_NOFILE)
                fds = range(4, s)

            for fd in fds:
                try: os.close(fd)
                except OSError: pass

        os.execl(_helper_path, "subprocmgr")

        # We should never get here.
        os._exit(127)

    except Exception as e:
        # Write a well-formed status message to the control socket
        # reporting our own failure to start up, and then exit abnormally.
        # Note that if we've already done the dup2() then we should use
        # fd 3 instead of the original fd, in case the original fd has
        # been closed.
        errc = getattr(e, 'errno', -1)
        text = str(e).encode("utf-8")
        msg = struct.pack("=4I{}s".format(len(text)),
                          0,    # pseudo-tag for the helper itself
                                # (code below will never use this)
                          1,    # status: system error during process creation
                          errc, # value: errno
                          len(text),
                          text)
        if not successful_dup2:
            control_socket.sendall(msg)
        else:
            socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM).sendall(msg)
        os._exit(1)

class Manager:
    """Internal: responsible for maintaining the communication channel to
       the C helper program, marshaling messages in both directions, etc.
       Usable as a context manager.
    """

    def __init__(self):
        self._helper_pid = None
        self._helper_skt = None
        self._decode_thr = None
        self._procs = {}
        self._stopping = False

    def __enter__(self):
        self.start_manager()

    def __exit__(self, et, ev, eb):
        self.stop_manager()
        return False

    def start_manager(self):
        if self._helper_pid is not None:
            return
        if self._stopping:
            raise RuntimeError("cannot start manager while it is being stopped")

        # Take care not to leak any resources if initialization fails
        s1 = None
        s2 = None
        pid = None
        thr = None
        try:
            (s1, s2) = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            pid = start_helper_process(s2)
            s2.close(); s2 = None
            thr = threading.Thread(
                target=self.decode_and_dispatch_status_messages,
                args=(s1,)
            )
            thr.run()

        except:
            if s1 is not None: s1.close()
            if s2 is not None: s2.close()
            if pid is not None: os.kill(pid, signal.SIGKILL)
            if thr is not None and thr.is_alive(): thr.join()
            raise

        self._helper_pid = pid
        self._helper_skt = skt
        self._decode_thr = thr


    def stop_manager(self):
        if self._helper_pid is None:
            return
        if self._stopping:
            return

        self._stopping = True

        # this notifies the helper program that it should exit
        self._helper_skt.shutdown(socket.SHUT_WR)

        (_, status) = os.waitpid(self._helper_pid, 0)
        if status:
            _log.warning("stop_manager: helper " +
                         interpret_wait_status(status))
        else:
            _log.debug("stop_manager: helper " +
                       interpret_wait_status(status))

        self._decode_thr.join()

        self._decode_thr = None
        self._helper_skt = None
        self._helper_pid = None
        self._stopping = False

    def decode_and_dispatch_status_messages(self, sock):
        """Thread worker procedure: receive status messages from the
           helper program and dispatch them to the appropriate Popen
           object's output queue."""
        ...

    def start_process(self, proc):
        """Actually start up the subprocess described by PROC."""
        ...

#
# Global state: unless otherwise specified, subprocesses created by this
# module are under this manager.  It will be started the first time it is
# needed, or when start_manager() is called.  It can be forcibly stopped
# by calling stop_manager().
#

_default_manager = Manager()
def start_manager():
    """Start the default subprocess manager now."""
    _default_manager.start_manager()

def stop_manager():
    """Stop the default subprocess manager now."""
    _default_manager.stop_manager()

atexit.atexit(stop_manager)

#
# Public classes
#

class SubprocessStatus:
    ...

class Popen:
    ...
