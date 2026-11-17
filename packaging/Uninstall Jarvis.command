#!/bin/bash
# Removes J.A.R.V.I.S. from this Mac: the program, the `jarvis` command and the Desktop launcher.
# Your settings, models and logs are kept unless you answer yes below.
APP="$HOME/Library/Application Support/JarvisApp"
DATA="$HOME/Library/Application Support/Jarvis"

rm -rf "$APP"
rm -f "$HOME/.local/bin/jarvis" "$HOME/Desktop/Jarvis.command"
echo "Jarvis has been removed."
if [ -d "$DATA" ]; then
    printf "Also delete settings, models, logs and face data in\n  %s ? (y/N) " "$DATA"
    read -r answer
    case "$answer" in
        y|Y|yes|YES) rm -rf "$DATA"; echo "Deleted." ;;
        *) echo "Kept." ;;
    esac
fi
security delete-generic-password -a jarvis -s "Jarvis Google connection" >/dev/null 2>&1 && echo "Removed the saved Google connection from your Keychain."
echo
read -n 1 -s -r -p "Press any key to close this window."
