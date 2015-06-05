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

   The convenience functions (`call`, `check_call`, `check_output`)
   are not implemented.  They wouldn't be hard to add, but if you need
   this module you probably aren't using them anyway.

   The module function `preinitialize` can be used to start up the C
   helper program and internal event loop, if this needs to happen
   before the first call to `subprocmgr.Popen` for some reason (e.g.
   if you want to do it before creating threads, or if you are going
   to change what stdin/stdout/stderr point to).

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
