#!/bin/bash

# Define local directories here
LOCAL_DIRS=(
    "$HOME/Documents"
    "$HOME/Books"
    "$HOME/Projects_toClean"
    ) 

# Define corresponding remote direcotries here
REMOTE_DIRS=(
    "edo-remote:Documents"
    "edo-remote:Books"
    "edo-remote:Projects_toClean"
)

# Function to log and print messages
log_message() {
    echo "$1"
    echo "$1" >> sync_gdrive.log
}

# Check if rclone is installed
if ! command -v rclone &> /dev/null; then
    log_message "rclone is not installed. Please install rclone and try again."
    exit 1
fi

log_message "rclone is installed."

# Check for rclone updates
log_message "Checking for rclone updates..."
CURRENT_VERSION=$(rclone version --check | grep "current version" | awk '{print $3}')
LATEST_VERSION=$(rclone version --check | grep "latest version" | awk '{print $3}')

if [ "$CURRENT_VERSION" != "$LATEST_VERSION" ]; then
    log_message "A new version of rclone is available: $LATEST_VERSION (current version: $CURRENT_VERSION)"
    read -p "Do you want to update rclone? (y/n): " UPDATE_CHOICE
    if [ "$UPDATE_CHOICE" = "y" ]; then
        log_message "Updating rclone..."
        curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip
        unzip rclone-current-linux-amd64.zip
        cd rclone-*-linux-amd64
        sudo cp rclone /usr/local/bin/
        cd ..
        rm -rf rclone-*-linux-amd64*
        log_message "rclone updated to the latest version: $(rclone version | grep "rclone v" | awk '{print $2}')"
    else
        log_message "Skipping rclone update."
    fi
else
    log_message "rclone is up to date."
fi

# Check if local directories exist
for DIR in "${LOCAL_DIRS[@]}"; do
    if [ ! -d "$DIR" ]; then
        log_message "Directory $DIR does not exist. Please create the directory and try again."
        exit 1
    fi
done

# Check if the number of local and remote directories match
if [ "${#LOCAL_DIRS[@]}" -ne "${#REMOTE_DIRS[@]}" ]; then
    log_message "The number of local directories does not match the number of remote directories. Please ensure they match and try again."
    exit 1
fi

log_message "All local directories exist."

# Sync local directories with their corresponding remote Google Drive directories
log_message "Starting sync with Google Drive..."
for i in "${!LOCAL_DIRS[@]}"; do
    LOCAL_DIR="${LOCAL_DIRS[$i]}"
    REMOTE_DIR="${REMOTE_DIRS[$i]}"
    log_message "Syncing $REMOTE_DIR to $LOCAL_DIR..."
    rclone sync --interactive "$LOCAL_DIR" "$REMOTE_DIR" -v
done

log_message "Sync completed successfully."
echo "Sync completed successfully. Check sync_gdrive.log for details."
