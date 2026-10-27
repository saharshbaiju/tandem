# Sourced by tandem, start-tandem.sh and install.sh: pick the Python to run with.
#
# The distro's own interpreter (/usr/bin/python3) is the one the dependencies
# are installed into (python-gobject / python3-gi / python3-gobject) and whose
# curses finds the system terminfo database. A `python3` that merely comes
# first on PATH — conda/miniconda, pyenv, a venv, Homebrew — has neither: it
# reports "python gi missing" no matter how often the package is installed, and
# conda's curses dies with "setupterm: could not find terminfo database".
#
# Override with TANDEM_PYTHON=/path/to/python3 (it then needs PyGObject itself).
tandem_python() {
  if [[ -n "${TANDEM_PYTHON:-}" ]]; then
    printf '%s\n' "$TANDEM_PYTHON"
  elif [[ -x /usr/bin/python3 ]]; then
    printf '%s\n' /usr/bin/python3
  else
    command -v python3
  fi
}
