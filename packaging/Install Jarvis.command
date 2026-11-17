#!/bin/bash
# Double-click me to install J.A.R.V.I.S. on this Mac.
# (If macOS says the file "cannot be opened": right-click it > Open > Open. Or run  bash install.sh  in Terminal.)
cd "$(dirname "$0")" || exit 1
bash ./install.sh
echo
read -n 1 -s -r -p "Press any key to close this window."
