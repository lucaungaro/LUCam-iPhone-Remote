#!/bin/bash
# LUCam iPhone Remote installer (macOS).
#
#   ./install.sh             install or update (asks before installing Homebrew)
#   ./install.sh --yes       don't ask, install whatever is missing
#   ./install.sh --uninstall remove the launchers (leaves Homebrew packages alone)
#
# Installs nothing that is already present. Needs: Python 3.8+ (to run the
# controller) and MediaMTX (only for the live view), both via Homebrew if missing.
# Run it again after moving this folder: the launchers point at its location.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_NAME="lucam-remote"
COMMAND_FILE="$APP_DIR/LUCam iPhone Remote.command"
YES=0
UNINSTALL=0
for arg in "$@"; do
	case "$arg" in
		-y|--yes) YES=1 ;;
		--uninstall) UNINSTALL=1 ;;
		-h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "Unknown option: $arg" >&2; exit 2 ;;
	esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
ask()  {
	[ "$YES" = 1 ] && return 0
	local answer
	{ read -r -p "  $1 [y/N] " answer </dev/tty; } 2>/dev/null || return 1
	[[ "$answer" =~ ^[Yy] ]]
}

[ "$(uname -s)" = "Darwin" ] || fail "This installer is for macOS. Elsewhere: install Python 3.8+ and MediaMTX, then run: python3 server.py"
[ "$(id -u)" != 0 ] || fail "Don't run this with sudo; Homebrew refuses to run as root."

find_brew() {
	command -v brew 2>/dev/null && return 0
	for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
		[ -x "$b" ] && { echo "$b"; return 0; }
	done
	return 1
}

launcher_dir() {
	local brew
	if brew="$(find_brew)"; then echo "$("$brew" --prefix)/bin"; else echo "$HOME/.local/bin"; fi
}

# Launchers from before the app was renamed (BM Cam Remote).
LEGACY_FILES=("$(launcher_dir)/bmcam-remote" "$HOME/.local/bin/bmcam-remote" "$APP_DIR/BM Cam Remote.command")

if [ "$UNINSTALL" = 1 ]; then
	bold "Removing LUCam iPhone Remote launchers"
	for f in "$(launcher_dir)/$LAUNCHER_NAME" "$HOME/.local/bin/$LAUNCHER_NAME" "$COMMAND_FILE" "${LEGACY_FILES[@]}"; do
		if [ -e "$f" ]; then rm -f "$f"; ok "removed $f"; fi
	done
	ok "Done. This folder, cameras.json and Homebrew packages were left in place."
	exit 0
fi

bold "LUCam iPhone Remote installer"
echo "  folder: $APP_DIR"

# --- Homebrew ---------------------------------------------------------------
bold "Homebrew"
BREW=""
if BREW="$(find_brew)"; then
	ok "found at $BREW"
else
	warn "Homebrew is not installed (it is needed to install MediaMTX for the live view)."
	if ask "Install Homebrew now? It may ask for your password."; then
		/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
		BREW="$(find_brew)" || fail "Homebrew install did not finish; run this script again."
		ok "installed at $BREW"
	else
		warn "skipped: the controller will work, the live view won't"
	fi
fi
[ -n "$BREW" ] && eval "$("$BREW" shellenv)"

# --- Python -------------------------------------------------------------------
bold "Python 3.8+"
python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; }
PYTHON=""
candidates=()
[ -n "$BREW" ] && candidates+=("$("$BREW" --prefix)/bin/python3")
candidates+=("$(command -v python3 2>/dev/null || true)")
for py in "${candidates[@]}"; do
	[ -n "$py" ] && [ -x "$py" ] || continue
	# /usr/bin/python3 is a stub that pops up an installer unless the
	# Command Line Tools are present: only use it when they are.
	if [ "$py" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
	if python_ok "$py"; then PYTHON="$py"; break; fi
done
if [ -z "$PYTHON" ]; then
	if [ -n "$BREW" ]; then
		warn "no usable Python found, installing it with Homebrew"
		"$BREW" install python
		PYTHON="$("$BREW" --prefix)/bin/python3"
		python_ok "$PYTHON" || fail "Python install failed"
	else
		fail "No Python 3.8+ found. Install Homebrew (re-run this script) or run: xcode-select --install"
	fi
fi
ok "$("$PYTHON" --version 2>&1) at $PYTHON"

# --- MediaMTX -------------------------------------------------------------------
bold "MediaMTX (live view)"
if command -v mediamtx >/dev/null 2>&1; then
	ok "found at $(command -v mediamtx)"
elif [ -n "$BREW" ]; then
	"$BREW" install mediamtx
	ok "installed $(mediamtx --version 2>/dev/null || true)"
else
	warn "not installed (no Homebrew): live view unavailable"
fi

# --- Launchers ----------------------------------------------------------------------
bold "Launchers"
for f in "${LEGACY_FILES[@]}"; do
	if [ -e "$f" ]; then rm -f "$f"; ok "removed old launcher $f"; fi
done
BIN="$(launcher_dir)"
mkdir -p "$BIN"
cat > "$BIN/$LAUNCHER_NAME" <<EOF
#!/bin/bash
# LUCam iPhone Remote launcher, written by $APP_DIR/install.sh
exec "$PYTHON" "$APP_DIR/server.py" "\$@"
EOF
chmod +x "$BIN/$LAUNCHER_NAME"
ok "command: $LAUNCHER_NAME  ($BIN/$LAUNCHER_NAME)"
case ":$PATH:" in
	*":$BIN:"*) ;;
	*) warn "$BIN is not on your PATH; add it in ~/.zshrc: export PATH=\"$BIN:\$PATH\"" ;;
esac

cat > "$COMMAND_FILE" <<EOF
#!/bin/bash
# Double-click to start LUCam iPhone Remote and open the control panel.
exec "$PYTHON" "$APP_DIR/server.py" --open
EOF
chmod +x "$COMMAND_FILE"
ok "double-click: $(basename "$COMMAND_FILE") (in this folder; drag it to the Dock or Desktop)"

# --- Check --------------------------------------------------------------------------
bold "Check"
"$PYTHON" -c "import sys; sys.path.insert(0, '$APP_DIR/python'); import lucam" || fail "the controller failed to load"
"$PYTHON" "$APP_DIR/server.py" --help >/dev/null || fail "server.py failed to start"
ok "controller loads"

echo
bold "Installed. Start it with:  $LAUNCHER_NAME --open"
echo "  or double-click 'LUCam iPhone Remote.command'. The panel is at http://localhost:9980"
echo "  and http://$(ipconfig getifaddr en0 2>/dev/null || echo '<this-mac-ip>'):9980 from other devices."
echo "  First run: allow 'Local Network' access when macOS asks, or cameras won't be found."
