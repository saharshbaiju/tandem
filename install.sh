#!/usr/bin/env bash
# Tandem — PC installer (Ubuntu / Fedora / Arch).
#
#   curl -fsSL https://raw.githubusercontent.com/saharshbaiju/tandem/main/install.sh | bash
#   # or, from a checkout:  ./install.sh
#
# Installs the Linux side into ~/.local/share/tandem, puts a `tandem` command in
# ~/.local/bin (so `tandem` works in any terminal), adds an app-menu entry, and installs the system packages your
# desktop needs (asks for sudo once). Re-run any time to update.
# Uninstall: ./install.sh --uninstall
set -euo pipefail

REPO="saharshbaiju/tandem"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/tandem"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

say()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$DEST" "$BIN/tandem" "$APPS/tandem.desktop"
  say "Removed Tandem (settings kept in ~/.config/tandem)."
  if [[ -e /etc/udev/rules.d/70-tandem-uinput.rules ]]; then
    say "Touch-input access rule left in place; remove it with:"
    say "  sudo rm /etc/udev/rules.d/70-tandem-uinput.rules /etc/modules-load.d/tandem-uinput.conf"
  fi
  exit 0
fi

# ---- 1. get the files: a local checkout, else the latest release, else main
SRC=""
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$here" && -f "$here/linux/tandemd.py" ]]; then
  SRC="$here/linux"
  say "Installing from this checkout ($here)"
else
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  url="https://github.com/$REPO/releases/latest/download/tandem-linux.tar.gz"
  # A 404 here just means no release has the tarball yet: quietly fall back.
  if ! curl -fsSL "$url" -o "$tmp/tandem.tar.gz" 2>/dev/null; then
    url="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
    say "No release found; using the main branch"
    curl -fsSL "$url" -o "$tmp/tandem.tar.gz"
  fi
  tar -xzf "$tmp/tandem.tar.gz" -C "$tmp"
  SRC="$(dirname "$(find "$tmp" -name tandemd.py -path '*linux*' | head -1)")"
  say "Downloaded $(basename "$url")"
fi

# ---- 2. copy into place (replacing an older install)
mkdir -p "$DEST" "$BIN" "$APPS"
rm -rf "$DEST/linux"
cp -r "$SRC" "$DEST/linux"
find "$DEST/linux" -name __pycache__ -prune -exec rm -rf {} +
# The license travels with the program (GPL-3.0; NOTICE has the extra terms).
for f in LICENSE NOTICE; do
  if [[ -f "$SRC/../$f" ]]; then cp "$SRC/../$f" "$DEST/$f"; fi
done
chmod +x "$DEST/linux/tandem" "$DEST/linux/start-tandem.sh"
ln -sf "$DEST/linux/tandem" "$BIN/tandem"

# Before the rename to Tandem this was installed as `amvd`; remove that copy.
old="${XDG_DATA_HOME:-$HOME/.local/share}/amvd"
if [[ -d "$old" || -L "$BIN/amvd" ]]; then
  rm -rf "$old" "$BIN/amvd" "$APPS/amvd.desktop" "$HOME/.config/fish/conf.d/amvd.fish"
  say "Removed the old amvd install (the command is now: tandem)"
fi

cat > "$APPS/tandem.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Tandem
Comment=Use an Android phone as a second monitor
Exec=$BIN/tandem
Icon=video-display
Terminal=true
Categories=Utility;
EOF

# ---- 3. system packages for this distro + desktop (same list the TUI checks)
cd "$DEST/linux"
# Every check below must run under the interpreter `tandem` runs under (the
# system one): asking a conda/pyenv python3 whether PyGObject is installed
# gives the wrong answer for the program that will actually use it.
source ./python.sh
PY="$(tandem_python)" || { warn "python3 is required"; exit 1; }
read -r -a PKGS <<<"$("$PY" -c 'import core; print(" ".join(core.missing_packages(core.Config().resolved_backend())))')"
if (( ${#PKGS[@]} )); then
  say "Installing packages: ${PKGS[*]}"
  if command -v apt-get >/dev/null; then sudo apt-get update -qq || true; fi
  CMD="$("$PY" -c 'import core,sys; c=core.install_command(sys.argv[1:]); print(" ".join(c) if c else "")' "${PKGS[@]}")"
  if [[ -n "$CMD" ]]; then
    $CMD || warn "Package install failed — open 'tandem' → Dependencies to retry."
  else
    warn "Unknown distro: install these yourself: ${PKGS[*]}"
  fi
else
  say "All required packages are already installed"
fi

backend="$("$PY" -c 'import core; print(core.Config().resolved_backend() or "")')"
case "$backend" in
  gnome)    say "Desktop: GNOME (Wayland)";;
  hyprland) say "Desktop: Hyprland";;
  *)        warn "No supported desktop detected in this session (GNOME on Wayland or Hyprland)."
            warn "On Ubuntu, pick \"Ubuntu\" (not \"Ubuntu on Xorg\") on the login screen.";;
esac

# ---- 4. firewall: the phone connects in on TCP 5000 over Wi-Fi / tethering
#         (the USB-debugging path goes through adb on 127.0.0.1 and needs nothing)
FW="$("$PY" -c '
import core
f = core.firewall_check(core.Config().port)
print((f["name"] + "|" + " ".join(f["fix"])) if f else "")')"
if [[ -n "$FW" ]]; then
  warn "${FW%%|*} may block the phone on Wi-Fi. To open the port:  ${FW#*|}"
  if [[ -r /dev/tty ]]; then
    read -r -p "Open it now? [Y/n] " ans </dev/tty || ans=n
    if [[ ! "$ans" =~ ^[Nn] ]]; then
      if "$PY" -c 'import core, subprocess, sys
f = core.firewall_check(core.Config().port)
sys.exit(subprocess.run(f["fix"]).returncode if f else 0)'; then
        say "Port opened"
      else
        warn "Couldn't open the port"
      fi
    fi
  fi
fi

# ---- 5. touch input (Hyprland only): the phone's touches are replayed through a
#         virtual touchscreen in /dev/uinput, which is root-only on most distros.
#         A udev rule grants it to whoever is logged in at the seat (uaccess).
#         GNOME needs nothing: Mutter's RemoteDesktop API injects the touches.
TOUCH_FIX="$("$PY" -c '
import core, shlex
t = core.touch_setup_check(core.Config())
print(shlex.join(t["fix"]) if t and t["fix"] else "")')"
if [[ -n "$TOUCH_FIX" ]]; then
  warn "Touch input needs access to /dev/uinput (one-time setup, asks for sudo):"
  warn "  $TOUCH_FIX"
  if [[ -r /dev/tty ]]; then
    read -r -p "Set it up now? [Y/n] " ans </dev/tty || ans=n
    if [[ ! "$ans" =~ ^[Nn] ]]; then
      if "$PY" -c 'import core, subprocess, sys
t = core.touch_setup_check(core.Config())
sys.exit(subprocess.run(t["fix"]).returncode if t else 0)'; then
        say "Touch input ready"
      else
        warn "Couldn't set up touch input; run 'tandem', choose Start Tandem and press t"
      fi
    fi
  fi
fi

# ---- 6. make `tandem` callable from any new terminal
on_path=1; case ":$PATH:" in *":$BIN:"*) ;; *) on_path=0;; esac
if (( ! on_path )); then
  line='export PATH="$HOME/.local/bin:$PATH"   # added by tandem installer'
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [[ -f "$rc" || "$rc" == "$HOME/.bashrc" ]] || continue
    grep -qs '.local/bin' "$rc" || { echo "$line" >> "$rc"; say "Added ~/.local/bin to PATH in $(basename "$rc")"; }
  done
  if [[ -d "$HOME/.config/fish" ]]; then
    mkdir -p "$HOME/.config/fish/conf.d"
    echo 'fish_add_path -g $HOME/.local/bin' > "$HOME/.config/fish/conf.d/tandem.fish"
  fi
fi
say "Done. Open a new terminal and run:  tandem"
(( on_path )) || say "(or right now:  $BIN/tandem)"
say "Phone app: https://github.com/$REPO/releases/latest  (Tandem-*.apk)"
