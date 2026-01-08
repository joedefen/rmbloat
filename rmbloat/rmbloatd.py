#!/usr/bin/env python3
BASH_SCRIPT_CONTENT = """#!/usr/bin/env bash
# rmbloatd.sh
# A dedicated tmux wrapper script to manage a persistent, long-running
# session for the rmbloat video converter tool.

# --- Configuration ---
DAEMON="rmbloat"           # The Python executable name (must be in PATH)
SESSION_NAME="rmbloat"     # Name of the tmux session
WINDOW_NAME="monitor"    # Name of the tmux window
PANE_TARGET="${SESSION_NAME}:${WINDOW_NAME}.0" # Specific pane (window 0, pane 0)
RMBLOAT_ARGS="$RMBLOAT_ARGS"  # Arguments to pass to rmbloat (set by Python wrapper)

# --- Utility Functions ---

is_pane_running_daemon() {
    # Check if the DAEMON process is actively running inside the target pane's TTY.
    # This bypasses the ambiguity of tmux's '#{pane_current_command}'.
    if ! tmux has-session -t ${SESSION_NAME} 2>/dev/null; then
        return 1
    fi

    # 1. Get the TTY (pseudo-terminal) associated with the target pane
    PANE_TTY=$(tmux display-message -p -t ${PANE_TARGET} '#{pane_tty}' 2>/dev/null)

    if [ -z "$PANE_TTY" ]; then
        return 1
    fi

    # 2. Check the process table for the DAEMON running on that TTY.
    # -t "$PANE_TTY": lists processes associated with the terminal device.
    # We grep for the DAEMON name and exclude the grep process itself.
    ps -t "$PANE_TTY" -o args= | grep "${DAEMON}" | grep -v "grep ${DAEMON}" | grep -q "${DAEMON}"

    return $?
}

d_start() {
    # Build the full command with optional arguments
    local DAEMON_CMD="${DAEMON}"
    if [ -n "$RMBLOAT_ARGS" ]; then
        DAEMON_CMD="${DAEMON} ${RMBLOAT_ARGS}"
    fi

    # 1. Check if the main tmux session exists
    if ! tmux has-session -t ${SESSION_NAME} 2>/dev/null; then
        echo "NOTE: Session '${SESSION_NAME}' not found. Creating and starting '${DAEMON}'."

        # Create a detached session, name the window, and start the DAEMON.
        # The 'bash' command keeps the pane open if rmbloat exits/crashes.
        tmux new-session -d -s ${SESSION_NAME} -n ${WINDOW_NAME} "${DAEMON_CMD}; bash"
    else
        # 2. Session exists, check if the DAEMON is running inside the pane
        if ! is_pane_running_daemon; then
            echo "NOTE: Session '${SESSION_NAME}' found, but '${DAEMON}' is NOT running in pane. Respawning..."

            # We need to find or create the target window/pane before respawning
            # A simpler approach is to always target pane 0, assuming the user doesn't mess with the layout.

            if ! tmux select-window -t ${SESSION_NAME}:${WINDOW_NAME} 2>/dev/null; then
                 # If window was killed but session is alive, create the window
                 tmux new-window -d -t ${SESSION_NAME}: -n ${WINDOW_NAME} "${DAEMON_CMD}; bash"
            else
                # Window exists, respawn the existing pane (0) in the target window, keeping it open with -k
                # Note: This kills the current process (likely the dormant bash shell) and starts rmbloat
                tmux respawn-pane -t ${PANE_TARGET} -k "${DAEMON_CMD}; bash"
            fi
        else
            echo "NOTE: '${DAEMON}' is already running persistently in session '${SESSION_NAME}'."
        fi
    fi
}

d_attach() {
    # Simply attach to the tmux session - fail if it doesn't exist
    if ! tmux has-session -t ${SESSION_NAME} 2>/dev/null; then
        echo "ERROR: tmux session '${SESSION_NAME}' does not exist."
        echo "Use 'rmbloatd start' to create it first."
        exit 1
    fi

    echo "Attaching to tmux session '${SESSION_NAME}'..."
    # Attempt to switch to the monitor window first
    tmux select-window -t ${SESSION_NAME}:${WINDOW_NAME} 2>/dev/null
    tmux attach-session -t ${SESSION_NAME}
}

d_stop() {
    echo -n "Stopping ${DAEMON} and killing session '${SESSION_NAME}'..."

    # Send CTRL-C (SIGINT) to the pane process to attempt a graceful exit
    tmux send-keys -t ${PANE_TARGET} C-c 2>/dev/null
    sleep 1

    # Hard kill the entire session for certainty, since the data is saved in rmbloat itself.
    if tmux has-session -t ${SESSION_NAME} 2>/dev/null; then
        tmux kill-session -t ${SESSION_NAME}
    fi
    echo " Done."
}

is_daemon_running_anywhere() {
    # Check if rmbloat is running anywhere (including outside tmux)
    # by checking for the existence of its lock file
    # Lock file location: /tmp/video_probes.json.lock (ProbeCache default)
    local LOCK_FILE="/tmp/video_probes.json.lock"

    if [ -f "$LOCK_FILE" ]; then
        # Lock file exists - check if it's actually locked (not stale)
        # Try to get a non-blocking exclusive lock
        if flock -n -x "$LOCK_FILE" -c "true" 2>/dev/null; then
            # We got the lock, which means it was stale/unlocked
            return 1
        else
            # Couldn't get lock - rmbloat is running
            return 0
        fi
    fi
    return 1
}


# --- Main Execution ---

# Early check for 'start' - fail if already running anywhere
if [ "$1" = "start" ]; then
    if is_daemon_running_anywhere; then
        echo "ERROR: '${DAEMON}' is already running (lock file exists)."
        echo "Use 'rmbloatd attach' to connect or 'rmbloatd stop' to stop it first."
        exit 1
    fi
fi

case "$1" in
    stop)
        d_stop
        ;;
    stat|status)
        if is_pane_running_daemon; then
            echo "NOTE: ${DAEMON} is running persistently in tmux session '${SESSION_NAME}'."
        else
            echo "NOTE: ${DAEMON} is NOT running persistently or session is dead."
        fi
        ;;
    start)
        d_start
        ;;
    attach)
        d_attach
        ;;
    *)
        echo "Usage: $0 {start|stop|status|attach}" >&2
        echo "" >&2
        echo "Commands:" >&2
        echo "  start  -- Start rmbloat in tmux (fails if already running)" >&2
        echo "            Only 'start' accepts arguments: rmbloatd start -- {rmbloat_args...}" >&2
        echo "            Example: rmbloatd start -- --auto-hr 2 /path/to/videos" >&2
        echo "" >&2
        echo "  attach -- Attach to tmux session (fails if session doesn't exist)" >&2
        echo "  stop   -- Stop rmbloat and kill tmux session" >&2
        echo "  status -- Check if rmbloat is running" >&2
        exit 1
        ;;
esac

exit 0
"""


def main():
    """
    Replaces the current Python process with a bash shell that executes
    the embedded script.

    Argument handling:
    - Only 'start' accepts rmbloat arguments
    - Args must come after "--" separator
    - Format: rmbloatd start -- {rmbloat_args...}
    """
    import os, sys

    # Executable path: The bash interpreter
    program = "/bin/bash"
    script_arg0 = 'rmbloatd'

    # Parse arguments to separate verb from rmbloat args
    args = sys.argv[1:]
    verb = None
    rmbloat_args = []

    # Look for "--" separator
    if '--' in args:
        sep_idx = args.index('--')
        if sep_idx == 0:
            # No verb provided before --
            print("ERROR: Verb required when providing rmbloat arguments", file=sys.stderr)
            print("Usage: rmbloatd start -- {rmbloat_args...}", file=sys.stderr)
            sys.exit(1)
        verb = args[0]
        rmbloat_args = args[sep_idx + 1:]

        # Only 'start' accepts arguments
        if verb != 'start':
            print(f"ERROR: Only 'start' accepts arguments, not '{verb}'", file=sys.stderr)
            print("Usage: rmbloatd start -- {rmbloat_args...}", file=sys.stderr)
            sys.exit(1)
    elif args:
        # No separator, just a verb
        verb = args[0]

    # Build environment variable for rmbloat args
    env = os.environ.copy()
    if rmbloat_args:
        # Join args with proper shell escaping
        import shlex
        env['RMBLOAT_ARGS'] = ' '.join(shlex.quote(arg) for arg in rmbloat_args)
    else:
        env['RMBLOAT_ARGS'] = ''

    # Replace the current process with the new command
    try:
        # Build exec args - only pass verb to bash script
        bash_args = [verb] if verb else []

        exec_args = [
            program,               # Path to the executable
            "-c",                  # Flag to execute the next string as a command
            BASH_SCRIPT_CONTENT,   # Command to execute (the script)
            script_arg0,           # This becomes $0 inside the script
            *bash_args             # The verb becomes $1
        ]

        os.execve(program, exec_args, env)

    except OSError as e:
        # This only runs if the execv call fails (e.g., /bin/bash not found)
        print(f"Error executing bash: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
