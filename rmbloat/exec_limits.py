#!/usr/bin/env python3
"""Apply resource limits and exec a command.

Run as a separate process from :func:`rmbloat.Sandbox.apply_limits`.  A helper
is used instead of ``subprocess``'s ``preexec_fn`` because that hook is unsafe
in a multithreaded process, and rmbloat always has TUI and worker threads.

The helper ``exec``s the target, so it never becomes a lingering parent.
Only limits that reduce runaway growth are supported; thread/process counts
are left to the container's ``--pids-limit`` because ``RLIMIT_NPROC`` is
per-user and would affect unrelated processes.
"""

import os
import resource
import sys


def _lower_soft(limit_name, value):
    """Lower the soft limit to ``value`` when the current limit allows it."""
    if value is None:
        return
    limit = getattr(resource, limit_name, None)
    if limit is None:
        return
    try:
        soft, hard = resource.getrlimit(limit)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        if soft != resource.RLIM_INFINITY and soft <= value:
            return
        resource.setrlimit(limit, (value, hard))
    except (OSError, ValueError):
        pass


def main(argv):
    core = 0
    fsize = None
    address_space = None
    nofile = None

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            index += 1
            break
        if arg == '--core':
            core = int(argv[index + 1])
            index += 2
        elif arg == '--fsize':
            fsize = int(argv[index + 1])
            index += 2
        elif arg == '--as':
            address_space = int(argv[index + 1])
            index += 2
        elif arg == '--nofile':
            nofile = int(argv[index + 1])
            index += 2
        else:
            break

    command = argv[index:]
    if not command:
        print('exec_limits: no command supplied', file=sys.stderr)
        return 2

    _lower_soft('RLIMIT_CORE', core)
    _lower_soft('RLIMIT_FSIZE', fsize)
    _lower_soft('RLIMIT_AS', address_space)
    _lower_soft('RLIMIT_NOFILE', nofile)

    os.execvpe(command[0], command, os.environ)
    return 127


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
