#!/usr/bin/env python3
import subprocess
import shutil
import os
from typing import Optional

# --- Configuration ---
CGROUP_NAME = "rmbloat_limit"
CGROUP_ROOT_PATH = "/sys/fs/cgroup" # Standard CGroup V2 root
CGROUP_PERIOD_US = 100000 # 100 milliseconds period

def _remove_cgroup_directory() -> None:
    """Attempts to remove the cgroup directory to ensure a clean setup."""
    cgroup_path = f"{CGROUP_ROOT_PATH}/{CGROUP_NAME}"

    if os.path.exists(cgroup_path):
        print(f"Attempting to remove existing CGroup directory: {cgroup_path}...")

        # We must use rmdir, but since it fails if not empty, we try a simple sudo rmdir.
        # This handles cases where the directory is empty but requires elevated permissions to remove.
        try:
            subprocess.run(["sudo", "rmdir", cgroup_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("   Previous directory successfully removed.")
        except subprocess.CalledProcessError:
            # If rmdir fails (usually "Directory not empty"), we warn but continue.
            print("   Warning: Existing CGroup directory could not be removed (may contain processes or files).")
            print("   Setup will attempt to overwrite existing files.")


def set_cgroup_cpu_limit(percent_limit: int) -> Optional[str]:
    """
    Creates or updates a persistent CGroup V2 hierarchy with a hard CPU limit.

    Args:
        percent_limit: The hard CPU limit (e.g., 300 for 300% / 3 cores).

    Returns:
        The 'cgexec' prefix command string if successful, or None if setup failed.
    """
    if percent_limit <= 0:
        return None

    # --- 1. Check for Dependencies & Clean State ---
    if not shutil.which("cgexec"):
        print("Error: Required utility 'cgexec' not found.")
        print("Please install the cgroup utilities (e.g., 'sudo apt install cgroup-tools' or 'sudo yum install libcgroup').")
        return None

    _remove_cgroup_directory() # Clean up before starting

    # Calculate desired Quota: (Limit / 100) * Period
    quota_us = int((percent_limit / 100) * CGROUP_PERIOD_US)
    cgroup_path = f"{CGROUP_ROOT_PATH}/{CGROUP_NAME}"
    cpu_max_file = os.path.join(cgroup_path, "cpu.max")
    cgroup_procs_file = os.path.join(cgroup_path, "cgroup.procs") # The file cgexec writes to
    subtree_control_file = os.path.join(CGROUP_ROOT_PATH, "cgroup.subtree_control")

    print(f"--- Setting up CGroup V2 '{CGROUP_NAME}' for {percent_limit}% CPU Limit ---")

    quota_value = f"{quota_us} {CGROUP_PERIOD_US}"

    # --- 2. Check Current State (Try to skip sudo) ---
    try:
        if os.path.exists(cpu_max_file):
            with open(cpu_max_file, 'r') as f:
                current_quota = f.read().strip()

            if current_quota == quota_value:
                print(f"--- CGroup is already correctly set: {quota_value}. Skipping sudo write. ---")
                cgexec_prefix = f"cgexec -g cpu:{CGROUP_NAME} "
                return cgexec_prefix
            else:
                print(f"--- CGroup exists but has incorrect quota ('{current_quota}'). Must overwrite. ---")

    except Exception as e:
        # Fall through to sudo steps if check fails
        print(f"Warning: Could not read current quota ({e}). Attempting setup via sudo...")
        pass

    # --- 3. Execute Setup (Requires Sudo) ---

    # A. MANDATORY: Delegate the CPU Controller to the root CGroup
    print("Delegating 'cpu' controller to the root CGroup...")
    delegate_cmd = ["sudo", "sh", "-c",
                    f"echo '+cpu' > {subtree_control_file}"]
    if subprocess.run(delegate_cmd, check=False).returncode != 0:
        print("\n-------------------------------------------------")
        print("CRITICAL ERROR: Failed to delegate 'cpu' controller. (Step A)")
        print("You may not have write permission to cgroup.subtree_control.")
        print("-------------------------------------------------")
        return None

    # A-1. VERIFICATION: Check if delegation succeeded
    try:
        with open(subtree_control_file, 'r') as f:
            if 'cpu' not in f.read():
                print("\n-------------------------------------------------")
                print("CRITICAL ERROR: CPU controller delegation failed verification. (Step A-1)")
                print("The system rejected the '+cpu' delegation request.")
                print("-------------------------------------------------")
                return None
        print("   Delegation verified: 'cpu' controller is active.")
    except Exception as e:
        print(f"Verification failed: Could not read {subtree_control_file}: {e}")
        return None

    # B. Create the CGroup directory (V2 hierarchy)
    create_dir_cmd = ["sudo", "mkdir", "-p", cgroup_path]
    if subprocess.run(create_dir_cmd, check=False).returncode != 0:
        print(f"Error: Failed to create CGroup directory at {cgroup_path}. Permission denied?")
        return None

    # C. Write Quota and Period to the V2 'cpu.max' file
    write_quota_cmd = ["sudo", "sh", "-c",
                       f"echo '{quota_value}' > {cpu_max_file}"]

    if subprocess.run(write_quota_cmd, check=False).returncode != 0:
        print("\n-------------------------------------------------")
        print("CRITICAL ERROR: Failed to write CPU quota to CGroup V2 file. (Step C)")
        print("This usually means the delegation in step A failed, or the directory setup is invalid.")
        print("-------------------------------------------------")
        return None

    # D. IMPORTANT: Change Ownership and Permissions for unprivileged execution
    print("Setting ownership and permissions for unprivileged execution...")

    # Get the current user name reliably using 'id -un'
    try:
        current_user = subprocess.run(["id", "-un"], check=True, capture_output=True, text=True).stdout.strip()
    except Exception:
        current_user = None

    if current_user:
        # D-1. Change Ownership (User:Group)
        chown_cmd = ["sudo", "chown", "-R", f"{current_user}:{current_user}", cgroup_path]
        if subprocess.run(chown_cmd, check=False).returncode != 0:
            print("Warning: Failed to change CGroup directory ownership (chown).")
        else:
            print(f"   Ownership set to {current_user}:{current_user}.")

        # D-2. Set Permissions on Directory (Ensure owner has read/write/execute rights)
        chmod_dir_cmd = ["sudo", "chmod", "u+rwx", cgroup_path]
        if subprocess.run(chmod_dir_cmd, check=False).returncode != 0:
            print("Warning: Failed to set CGroup directory permissions (chmod).")
        else:
            print("   Directory permissions set (u+rwx).")

        # D-3. CRITICAL: Set Permissions on cgroup.procs file (cgexec target)
        # This ensures the non-root owner can write their PID to the file.
        chmod_procs_cmd = ["sudo", "chmod", "u+w", cgroup_procs_file]
        if subprocess.run(chmod_procs_cmd, check=False).returncode != 0:
            print("Warning: Failed to set write permission on cgroup.procs file.")
        else:
            print("   Write permission set on cgroup.procs.")
    else:
        print("Warning: Could not reliably determine current user. Skipping chown/chmod.")

    print(f"--- CGroup setup complete. Quota set to {quota_us}us (3 cores). ---")

    # 4. Return the cgexec prefix string (simplified)
    cgexec_prefix = f"cgexec -g cpu:{CGROUP_NAME} "
    print("\nUse the following prefix to launch your FFmpeg job (append your ionice/nice/ffmpeg commands):")
    print(cgexec_prefix)

    return cgexec_prefix

# --- Example Usage ---
if __name__ == "__main__":
    TARGET_LIMIT = 300

    prefix = set_cgroup_cpu_limit(TARGET_LIMIT)

    if prefix:
        print(f"\nSUCCESS: Use this prefix for your subprocess command lists: '{prefix}'")
    else:
        print("\nFAILURE: CGroup setup failed.")