import os
import subprocess
import sys

# Define local directories here
LOCAL_DIRS = [
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Books"),
    os.path.expanduser("~/Projects_toClean")
]

# Define corresponding remote directories here
REMOTE_DIRS = [
    "edo-remote:Documents",
    "edo-remote:Books",
    "edo-remote:Projects_toClean"
]

# Resolved next to this script, not the working directory: cron starts with an arbitrary
# working directory, and a relative path would scatter the log (or fail) depending on where
# the job was launched from.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_gdrive.log")

# Function to log and print messages
def log_message(message):
    print(message)
    with open(LOG_FILE, "a", encoding="utf-8") as log_file:
        log_file.write(message + "\n")

# Check if rclone is installed
def check_rclone_installed():
    try:
        subprocess.run(["rclone", "version"], check=True, capture_output=True)
    except FileNotFoundError:
        log_message("rclone is not installed. Please install rclone and try again.")
        # TODO: Run script of installation for rclone
        sys.exit(1)
    except subprocess.CalledProcessError as error:
        log_message(f"rclone is installed but 'rclone version' failed: {error}")
        sys.exit(1)
    log_message("rclone is installed.")


# Check if local directories exist
def check_local_directories():
    for directory in LOCAL_DIRS:
        if not os.path.isdir(directory):
            log_message(
                f"Directory {directory} does not exist. Please create the directory and try again."
            )
            sys.exit(1)

    if len(LOCAL_DIRS) != len(REMOTE_DIRS):
        log_message(
            "The number of local directories does not match the number of remote "
            "directories. Please ensure they match and try again."
        )
        sys.exit(1)

    log_message("All local directories exist.")

def parse_sync_output():
    # TODO: handle parsing of the output
    pass

# Sync local directories with their corresponding remote Google Drive directories
def sync_directories():
    log_message("Starting sync with Google Drive...")
    failures = []
    for local_dir, remote_dir in zip(LOCAL_DIRS, REMOTE_DIRS):
        log_message(f"Syncing {remote_dir} to {local_dir}...")
        try:
            subprocess.run(
                ["rclone", "sync", "--interactive", local_dir, remote_dir, "-v"],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as error:
            failures.append(remote_dir)
            log_message(
                f"Sync failed for {remote_dir}: rclone exited with status {error.returncode}"
            )
            if error.stderr:
                log_message(error.stderr.decode("utf-8", "replace").strip())
        except OSError as error:
            failures.append(remote_dir)
            log_message(f"Sync failed for {remote_dir}: {error}")

    if failures:
        log_message(f"Sync finished with {len(failures)} failure(s): {', '.join(failures)}")
        print("Sync finished with failures. Check sync_gdrive.log for details.")
        return 1

    log_message("Sync completed successfully.")
    print("Sync completed successfully. Check sync_gdrive.log for details.")
    return 0

def main():
    check_rclone_installed()
    # check_rclone_updates() # TODO
    check_local_directories()
    return sync_directories()

if __name__ == "__main__":
    sys.exit(main())
