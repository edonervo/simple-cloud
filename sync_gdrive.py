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

# Function to log and print messages
def log_message(message):
    print(message)
    with open("sync_gdrive.log", "a") as log_file:
        log_file.write(message + "\n")

# Check if rclone is installed
def check_rclone_installed():
    try:
        subprocess.run(["rclone", "version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        log_message("rclone is installed.")
    except FileNotFoundError:
        log_message("rclone is not installed. Please install rclone and try again.")
        sys.exit(1)

# Check for rclone updates
# def check_rclone_updates():
    # log_message("Checking for rclone updates...")
    # result = subprocess.run(["rclone", "version", "--check"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # output = result.stdout.splitlines()
    
    # current_version = next(line for line in output if "current version" in line).split()[-1]
    # latest_version = next(line for line in output if "latest version" in line).split()[-1]

    # if current_version != latest_version:
    #     log_message(f"A new version of rclone is available: {latest_version} (current version: {current_version})")
    #     update_choice = input("Do you want to update rclone? (y/n): ").strip().lower()
    #     if update_choice == "y":
    #         log_message("Updating rclone...")
    #         subprocess.run("curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip", shell=True, check=True)
    #         subprocess.run("unzip rclone-current-linux-amd64.zip", shell=True, check=True)
    #         subprocess.run("cd rclone-*-linux-amd64 && sudo cp rclone /usr/local/bin/ && cd .. && rm -rf rclone-*-linux-amd64*", shell=True, check=True)
    #         result = subprocess.run(["rclone", "version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    #         new_version = next(line for line in result.stdout.splitlines() if "rclone v" in line).split()[1]
    #         log_message(f"rclone updated to the latest version: {new_version}")
    #     else:
    #         log_message("Skipping rclone update.")
    # else:
    #     log_message("rclone is up to date.")

# Check if local directories exist
def check_local_directories():
    for directory in LOCAL_DIRS:
        if not os.path.isdir(directory):
            log_message(f"Directory {directory} does not exist. Please create the directory and try again.")
            sys.exit(1)

    if len(LOCAL_DIRS) != len(REMOTE_DIRS):
        log_message("The number of local directories does not match the number of remote directories. Please ensure they match and try again.")
        sys.exit(1)

    log_message("All local directories exist.")

# Sync local directories with their corresponding remote Google Drive directories
def sync_directories():
    log_message("Starting sync with Google Drive...")
    for local_dir, remote_dir in zip(LOCAL_DIRS, REMOTE_DIRS):
        log_message(f"Syncing {remote_dir} to {local_dir}...")
        subprocess.run(["rclone", "sync", "--interactive", local_dir, remote_dir, "-v"], check=True)
    log_message("Sync completed successfully.")
    print("Sync completed successfully. Check sync_gdrive.log for details.")

if __name__ == "__main__":
    check_rclone_installed()
    # check_rclone_updates()
    check_local_directories()
    sync_directories()
