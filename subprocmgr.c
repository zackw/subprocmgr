/* subprocmgr - Subprocess manager (C component).
 *
 * Copyright © 2015 Zack Weinberg
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 * http://www.apache.org/licenses/LICENSE-2.0
 * There is NO WARRANTY.
 */

/* This program, together with the Python module that uses it, solves
   one very specific headache when writing a Python program that runs
   a bunch of subprocesses and consumes their output: There is no way
   to select() for the termination of a specific process, and
   therefore no reliable way to know when to call wait().

   This program is not meant to be invoked directly.  It communicates
   with the Python module using an internal protocol that is subject
   to change without notice, but which I will document here anyway.
   It takes no arguments, and will never write anything to stdout or
   read anything from stdin.  Human-readable error messages may be
   written to stderr under unusual failure conditions.

   On invocation, file descriptor 3 must be an AF_UNIX/SOCK_STREAM
   socket, referred to as the "control socket", which is used to
   instruct this program to create new subprocesses.  Abstractly,
   there is only one type of message sent to this program on this
   socket, but it's processed as a pair of sub-messages.  The first
   sub-message of each pair is expected to consist of 2 32-bit,
   native-endian integers in this order:

       data_len
       n_fds

   The second sub-message consists of 'data_len' bytes of ordinary
   data, plus 'n_fds' file descriptors as SCM_RIGHTS data.  At
   present, the second sub-message will be ill-formed if it does not
   provide at least 16 bytes of data and one file descriptor.

   The second sub-message's data has the format

       uint32_t   tag
       uint8_t    flags
       uint8_t    disposition of fd 0 in subprocess
       uint8_t    disposition of fd 1 in subprocess
       uint8_t    disposition of fd 2 in subprocess
       uint32_t   argument count
       uint32_t   environment variable count
       cstring    name of executable
       cstring[]  argument vector
       cstring[]  environment vector

   The file descriptors passed, if any, are a simple array, referenced
   by the "disposition of fd N in subprocess" fields of the message.

   'tag' is an arbitrary, invoker-selected value used to distinguish
   processes in status messages.  It is the invoker's responsibility
   not to reuse tags while their associated processes are still alive.

   'flags' is currently reserved and must be all-bits-zero.

   The "disposition of fd N in subprocess" codes have the following
   possible values.  Only the value -1 (== 0xFF) is treated as negative.

       -1         Inherit from parent.
       0          fd 0:   Open /dev/null for read.
                  fd 1/2: Forward output.
       k >= 1     Use passed fd with index (k-1).

   Currently it is not possible to supply higher-numbered file descriptors
   to the child.

   The executable name, argument vector, and environment vector are
   all packed together as a sequence of C-strings; there is no
   formatting other than the NUL-terminators.  The executable name is
   mandatory but the other two are optional.  The argument count and
   environment variable count give the number of entries in their
   respective vectors, with two special cases: If the argument count
   is zero, the executable name will be reused as the sole entry in
   the argument vector passed to execve(2).  If the environment
   variable count is -1 (== 0xFFFF_FFFF), the environment vector is
   expected to be empty, and the new process will inherit its
   environment from the parent (i.e. this program).

   (If the environment variable count is zero, the new process will
   recieve a completely empty environment.)

   This program sends "status messages" back to the invoker via the
   control socket.  These messages consist of 4 unsigned 32-bit
   integers followed by zero or more bytes of data:

       tag
       status
       value
       len

   'tag' is always the tag provided with the message that created the
   process, and 'len' always indicates how many bytes of data follow.
   The meaning of 'value' depends on 'status', which is one of the
   following possible codes:

       0    The control message was ill-formed.  'value' is zero,
            and the data is a human-readable message describing the problem.

       1    System error during process creation. 'value' is an errno code
            and the data is a human-readable error message.  This message
            will include strerror(value).

       2    Process successfully created.  'value' is the process ID.
            No data.

       3    Process has produced output.  'value' will be 1 for stdout
            or 2 for stderr, and the data is a block of output.  This
            program does not reblock or transform the data in any way;
            one chunk of data read from the pipe = one message.

       4    Process has closed an output channel. 'value' will be 1 for
            stdout or '2' for stderr.  No data.

       5    Process has exited.  'value' is the wait status.  No data.

   For any given process, this program guarantees to emit messages in
   the following order: First, exactly one message with status 0, 1,
   or 2.  If the code was 0 or 1, there will be no further messages
   for that tag, and all passed file descriptors have been closed.
   Otherwise, any number of messages with status 3, followed by
   exactly one message with status 4, for whichever of stdout, stderr,
   or both were given disposition "Forward output via the status
   pipe".  (There is no ordering between stdout and stderr.)  Finally,
   exactly one message with status 5.

   When EOF is received on the control socket, this program will send
   SIGTERM to all processes that are still running, and start a
   five-second timer.  It will continue to generate status messages
   until there are no more messages to generate (i.e. all children
   have exited), and then it will exit itself.  If the timer expires,
   any surviving processes receive a SIGKILL and status message
   generation continues.

   If this program ever receives a write error on the control socket,
   all further output from subprocesses, and their wait statuses,
   will be read and discarded.  This condition does *not* cause running
   subprocesses to be terminated.

   If this program ever receives SIGHUP, SIGINT, SIGQUIT, SIGALRM,
   SIGTERM, SIGVTALRM, SIGXCPU, SIGXFSZ, or SIGPWR, it will behave as
   if it had received EOF on the control socket, except that the
   initial signal sent is the same as was received.  If this program
   receives SIGILL, SIGABRT, SIGFPE, SIGBUS, SIGSEGV, SIGSYS, or
   SIGTRAP, it will immediately send SIGKILL to all processes that are
   still running, and then crash as usual for that signal.
   The signals that stop processes are allowed to behave normally.
   All other signals are ignored.

   (Child processes receive whatever signal mask this program's parent
   provided it.)
 */

/* Portability note: this program makes use of many POSIX.1-2008
   (including XSI) APIs and several APIs that are currently
   Linux-specific.  */

#define _GNU_SOURCE
#include <stddef.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <errno.h>

#include <sys/types.h>
#include <sys/socket.h>
#include <sys/epoll.h>
#include <fcntl.h>
#include <unistd.h>
