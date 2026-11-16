#!/usr/bin/env bash
# Tandem — PC installer: Arch, Fedora, Debian / Ubuntu and their derivatives
# (CachyOS, EndeavourOS, Kubuntu, KDE neon, Mint…), for Hyprland, GNOME or
# KDE Plasma.
#
#   curl -fsSL https://raw.githubusercontent.com/saharshbaiju/tandem/main/install.sh | bash
#   # or, from a checkout:  ./install.sh
#   # a specific release, e.g. a beta (pre-releases are never "latest"):
#   curl -fsSL https://raw.githubusercontent.com/saharshbaiju/tandem/v0.7.0/install.sh | TANDEM_VERSION=v0.7.0 bash
#
# Installs the Linux side into ~/.local/share/tandem, puts a `tandem` command in
# ~/.local/bin (so `tandem` works in any terminal), adds an app-menu entry, and installs the system packages your
# desktop needs (asks for sudo once). On KDE Plasma it also builds Tandem's small
# KWin helper and registers it with KWin. Re-run any time to update.
# Uninstall: ./install.sh --uninstall
set -euo pipefail

REPO="saharshbaiju/tandem"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/tandem"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

say()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

if [[ "${1:-}" == "--uninstall" ]]; then
  # $DEST also holds the built KWin helper (tandem/kwin-helper/). Its desktop
  # entry is the one other file Tandem writes, in KWin's own data directory,
  # which backends/kwin_helper.py knows how to find; the rm below covers an
  # install too old (or too broken) to ask.
  if [[ -f "$DEST/linux/backends/kwin_helper.py" ]] && command -v python3 >/dev/null; then
    (cd "$DEST/linux" && python3 -c 'from backends import kwin_helper; kwin_helper.remove_installed()') 2>/dev/null \
      || warn "Couldn't remove the KWin helper's desktop entry (tandem-kwin-helper.desktop); delete it by hand"
  fi
  rm -rf "$DEST" "$BIN/tandem" "$APPS/tandem.desktop" "$APPS/tandem-kwin-helper.desktop"
  say "Removed Tandem (settings kept in ~/.config/tandem)."
  if [[ -e /etc/udev/rules.d/70-tandem-uinput.rules ]]; then
    say "Touch-input access rule left in place; remove it with:"
    say "  sudo rm /etc/udev/rules.d/70-tandem-uinput.rules /etc/modules-load.d/tandem-uinput.conf"
  fi
  exit 0
fi

# ---- 1. get the files: a local checkout, else the release TANDEM_VERSION
#         names (a tag such as v0.7.0), else the latest release, else
#         main. GitHub's releases/latest skips pre-releases, so naming the tag
#         is how testers install a beta.
VERSION="${TANDEM_VERSION:-}"
if [[ -n "$VERSION" && ! "$VERSION" =~ ^[A-Za-z0-9._-]+$ ]]; then
  warn "TANDEM_VERSION must be a release tag such as v0.7.0 (got: $VERSION)"
  exit 1
fi
SRC=""
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$here" && -f "$here/linux/tandemd.py" ]]; then
  SRC="$here/linux"
  say "Installing from this checkout ($here)"
  [[ -z "$VERSION" ]] || warn "TANDEM_VERSION is ignored when installing from a checkout"
else
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  if [[ -n "$VERSION" ]]; then
    # No fallback: installing anything but the release asked for would hide
    # a typo in the tag.
    url="https://github.com/$REPO/releases/download/$VERSION/tandem-linux.tar.gz"
    if ! curl -fsSL "$url" -o "$tmp/tandem.tar.gz"; then
      warn "Release $VERSION has no tandem-linux.tar.gz; see https://github.com/$REPO/releases"
      exit 1
    fi
  else
    url="https://github.com/$REPO/releases/latest/download/tandem-linux.tar.gz"
    # A 404 here just means no release has the tarball yet: quietly fall back.
    if ! curl -fsSL "$url" -o "$tmp/tandem.tar.gz" 2>/dev/null; then
      url="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
      say "No release found; using the main branch"
      curl -fsSL "$url" -o "$tmp/tandem.tar.gz"
    fi
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
rm -rf "$DEST/linux/kwin-helper/build"   # a hand-made `make` build in a checkout
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
  gnome)    say "Desktop: GNOME${WAYLAND_DISPLAY:+ (Wayland)}";;
  hyprland) say "Desktop: Hyprland";;
  kde)      say "Desktop: KDE Plasma${WAYLAND_DISPLAY:+ (Wayland)}";;
  *)        warn "No supported desktop detected in this session (run $("$PY" -c 'import backends; print(backends.supported_desktops())' 2>/dev/null || echo "Hyprland, GNOME or KDE Plasma"))."
            warn "On Ubuntu, pick \"Ubuntu\" (not \"Ubuntu on Xorg\") on the login screen; on Plasma, \"Plasma (Wayland)\".";;
esac
# Why this session can't make a virtual monitor (GNOME or Plasma on X11…), as
# the TUI's start check says it, and whether this desktop's support is in
# beta (none is right now). Everything is inside the try: a release tarball older than this
# script has no backends.module() or backends.beta_notice() yet.
if [[ -n "$backend" ]]; then
  "$PY" -c '
import sys
try:
    import backends
    hooks = backends.module(sys.argv[1])
    lines = hooks.session_problem() if hooks else None
except Exception:
    lines = None
for line in lines or []:
    print(line)' "$backend" | while IFS= read -r line; do warn "$line"; done || true
  beta_notice="$("$PY" -c '
import sys
try:
    import backends
    print(backends.beta_notice(sys.argv[1]) or "")
except Exception:
    pass' "$backend" || true)"
  [[ -z "$beta_notice" ]] || say "Note: $beta_notice"
fi

# ---- 3b. KDE Plasma: build the KWin helper and register it with KWin. The
#          daemon would do both on its first start; doing it now surfaces a
#          missing compiler here, and lets KWin 6.7 and older read the helper's
#          desktop entry before Tandem first needs it.
if [[ "$backend" == "kde" ]]; then
  if "$PY" -c '
import sys
from backends import kwin_helper
try:
    kwin_helper.ensure_desktop_entry(kwin_helper.ensure_built())
except RuntimeError as error:
    print(error, file=sys.stderr)
    sys.exit(1)'; then
    say "KWin helper ready ($("$PY" -c 'from backends import kwin_helper; print(kwin_helper.helper_path())'))"
  else
    warn "Couldn't build the KWin helper (the reason is above). Tandem needs it on KDE Plasma:"
    warn "install what 'tandem' → Dependencies lists as missing; the next start builds it."
  fi
fi

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

# ---- 5. touch input: one-time setup, as the backend reports it
#         (core.touch_setup_check -> the backend's touch_setup_problem hook).
#         Hyprland replays touches through a virtual touchscreen in
#         /dev/uinput, root-only on most distros: a udev rule grants it to
#         whoever is logged in at the seat (uaccess; asks for sudo). GNOME and
#         KDE Plasma inject touches through the compositor and need nothing.
# Prints "<problem><TAB><fix>" (fix empty when there is nothing to run), or
# nothing when touch needs no setup.
TOUCH="$("$PY" -c '
import core, shlex
t = core.touch_setup_check(core.Config())
if t:
    print(t["problem"] + "\t" + (shlex.join(t["fix"]) if t["fix"] else ""))' || true)"
if [[ -n "$TOUCH" ]]; then
  problem="${TOUCH%%$'\t'*}"; fix="${TOUCH#*$'\t'}"
  if [[ -z "$fix" ]]; then
    warn "Touch input: $problem"
  else
    if [[ "$fix" == sudo\ * ]]; then
      warn "Touch input needs one-time setup ($problem; asks for sudo):"
    else
      warn "Touch input needs one-time setup ($problem):"
    fi
    warn "  $fix"
    if [[ -r /dev/tty ]]; then
      read -r -p "Set it up now? [Y/n] " ans </dev/tty || ans=n
      if [[ ! "$ans" =~ ^[Nn] ]]; then
        if "$PY" -c 'import core, subprocess, sys
t = core.touch_setup_check(core.Config())
sys.exit(subprocess.run(t["fix"]).returncode if t and t["fix"] else 0)'; then
          say "Touch input ready"
        else
          warn "Couldn't set up touch input; run 'tandem', choose Start Tandem and press t"
        fi
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
if [[ -n "$VERSION" ]]; then
  say "Phone app: https://github.com/$REPO/releases/tag/$VERSION  (tap Tandem.apk)"
else
  say "Phone app: https://github.com/$REPO/releases/latest  (tap Tandem.apk)"
fi
