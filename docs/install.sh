#!/bin/bash
# ============================================================================
#  J.A.R.V.I.S. installer for macOS 12+ (Apple silicon or Intel)
#
#  Either double-click "Install Jarvis.command" from the installer zip, or paste this in Terminal:
#      curl -fsSL https://ryu10867.github.io/jarvis/install.sh | bash
#
#  Program  ->  ~/Library/Application Support/JarvisApp   (its own Python environment)
#  Settings, models, logs  ->  ~/Library/Application Support/Jarvis
#  Adds the `jarvis` command (~/.local/bin) and a "Jarvis" launcher on the Desktop.
#  Nothing is installed system-wide and nothing needs sudo.
# ============================================================================
set -euo pipefail

REPO="Ryu10867/jarvis"
APP="$HOME/Library/Application Support/JarvisApp"
VENV="$APP/venv"
BIN="$HOME/.local/bin"
SETUP_ARGS="${JARVIS_SETUP_ARGS:-}"          # e.g. "--yes" to accept every default without asking

say()  { printf "\033[36m%s\033[0m\n" "$*"; }
note() { printf "\033[90m%s\033[0m\n" "$*"; }
warn() { printf "\033[33m%s\033[0m\n" "$*"; }
die()  { printf "\033[31m%s\033[0m\n" "$*" >&2; exit 1; }

printf "\n  J.A.R.V.I.S. installer\n\n"
[ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS. On Windows, use install.bat."
major="$(sw_vers -productVersion | cut -d. -f1)"
[ "$major" -ge 12 ] || die "macOS 12 (Monterey) or newer is needed; this Mac runs $(sw_vers -productVersion)."

# --- 1. the package itself: next to this script (installer zip), or fetched from the latest release
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
WHEEL=""
if [ -n "$HERE" ]; then
    WHEEL="$(ls "$HERE"/jarvis_assistant-*.whl 2>/dev/null | sort | tail -1 || true)"
fi
if [ -z "$WHEEL" ]; then
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    say "Downloading Jarvis..."
    curl -fL --progress-bar "https://github.com/$REPO/releases/latest/download/Jarvis-Installer-macOS.zip" -o "$TMP/jarvis.zip" \
        || die "Could not download the installer. Check your internet connection and try again."
    unzip -q "$TMP/jarvis.zip" -d "$TMP"
    WHEEL="$(ls "$TMP"/jarvis_assistant-*.whl 2>/dev/null | sort | tail -1 || true)"
    [ -n "$WHEEL" ] || die "The download did not contain the Jarvis package."
fi

# --- 2. Python 3.11+ with Tk (for the window). Apple's own /usr/bin/python3 is skipped: it is too old, and on a Mac
#        without the developer tools merely touching it pops up an "install tools" dialog.
find_python() {
    local c p
    for c in python3.13 python3.12 python3.11 python3.14 python3; do
        p="$(command -v "$c" 2>/dev/null || true)"
        [ -n "$p" ] || continue
        case "$p" in /usr/bin/*) continue ;; esac
        if "$p" -c 'import sys, venv, tkinter, ssl; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)' 2>/dev/null; then
            echo "$p"
            return 0
        fi
    done
    return 1
}
PY="$(find_python || true)"
if [ -z "$PY" ]; then
    say "Getting Python 3.12 (a private copy, through the uv tool; your system is not changed)..."
    export PATH="$BIN:$HOME/.cargo/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 \
            || die "Could not install uv. Install Python 3.12 from https://www.python.org/downloads/ and run this again."
    fi
    uv python install 3.12 >/dev/null 2>&1 || die "Could not download Python 3.12."
    PY="$(uv python find 3.12)"
fi
note "Using Python: $PY ($("$PY" -c 'import platform; print(platform.python_version(), platform.machine())'))"

# --- 3. a private environment + the package and its libraries (~400 MB download the first time)
mkdir -p "$APP" "$BIN"
if [ ! -x "$VENV/bin/python" ]; then
    say "Creating Jarvis's Python environment..."
    "$PY" -m venv "$VENV" || die "Could not create the environment in $VENV"
fi
say "Installing Jarvis and its libraries (this can take a few minutes)..."
"$VENV/bin/python" -m pip install --upgrade pip --disable-pip-version-check -q
"$VENV/bin/python" -m pip install --upgrade --disable-pip-version-check "$WHEEL" \
    || die "pip install failed (see the messages above)."

# --- 4. the `jarvis` command
cat > "$BIN/jarvis" <<EOF
#!/bin/bash
# Starts J.A.R.V.I.S. (installed by install.sh)
VENV="$VENV"
CERTS="\$("\$VENV/bin/python" -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
[ -n "\$CERTS" ] && export SSL_CERT_FILE="\$CERTS"       # so downloads work with any Python build
exec "\$VENV/bin/python" -m jarvis "\$@"
EOF
chmod +x "$BIN/jarvis"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *)
        for rc in "$HOME/.zprofile" "$HOME/.bash_profile"; do
            if [ "$rc" = "$HOME/.zprofile" ] || [ -f "$rc" ]; then
                grep -qs '.local/bin' "$rc" || printf '\n# Jarvis\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
            fi
        done
        note "Added ~/.local/bin to your PATH (open a new Terminal window to use 'jarvis')."
        ;;
esac

# --- 5. a double-clickable launcher on the Desktop (opens in Terminal, which is what macOS gives the microphone
#        and Accessibility permissions to)
if [ -d "$HOME/Desktop" ]; then
    cat > "$HOME/Desktop/Jarvis.command" <<EOF
#!/bin/bash
clear
exec "$BIN/jarvis"
EOF
    chmod +x "$HOME/Desktop/Jarvis.command"
fi

printf "\n\033[32mJarvis is installed. Starting first-time setup...\033[0m\n\n"
# When this script arrives through "curl ... | bash" its own input is the script, so questions read the keyboard directly.
if [ -n "$SETUP_ARGS" ]; then
    # shellcheck disable=SC2086
    "$BIN/jarvis" setup $SETUP_ARGS
elif [ -r /dev/tty ] && [ -t 1 ]; then
    "$BIN/jarvis" setup </dev/tty
else
    "$BIN/jarvis" setup --yes
fi

printf "\n\033[32mAll done.\033[0m Double-click 'Jarvis' on the Desktop, or type  jarvis  in a new Terminal window.\n"
printf "The first time, macOS will ask to let Terminal use the microphone: say yes, or Jarvis can't hear you.\n"
