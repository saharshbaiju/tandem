# Unit tests

Tests for the Linux side (`linux/`), using only the standard library
(`unittest` and `unittest.mock`). They need no compositor, phone, GPU or
GStreamer: D-Bus calls, compositor queries and the GStreamer and KDE tools
(`gst-launch-1.0`, `gst-inspect-1.0`, `kscreen-doctor`) are mocked. A few
tests do run real programs, all harmless:

* `sleep`, and small `sh` / Python scripts that stand in for an encoder or for
  `tandem-kwin-helper` (speaking its real line protocol);
* `bash -n install.sh`, a syntax check that runs nothing, and a copy of
  `install.sh` in a temporary directory up to its download step, with a
  stand-in `curl` that only records the URLs (nothing is downloaded or
  installed);
* a real build of `tandem-kwin-helper` with `wayland-scanner`, `pkg-config`
  and `cc`, only when those are installed (skipped otherwise), into a
  temporary directory, then the built helper with `--help`, bad arguments,
  and a Wayland display that doesn't exist;
* a real UNIX socket in a temporary directory, for the socket-owner lookup
  through `SO_PEERCRED`.

Run them from the repository root with the system Python (the one Tandem
itself runs on):

```bash
/usr/bin/python3 -m unittest discover -s tests -v
```

One module, or one test:

```bash
/usr/bin/python3 -m unittest discover -s tests -p test_layout.py -v
/usr/bin/python3 -m unittest discover -s tests -k PlaceBeside -v
```

They live here, not in `linux/`, because `install.sh` copies `linux/` to every
user's machine.

Every test module starts with `import support`. `support.py` puts `linux/` on
`sys.path` and points `HOME` and the XDG directories at a throwaway directory
before any Tandem module is imported, so a test run never reads or changes your
pairing key, certificate or config.

| Module | Covers |
|---|---|
| `test_backends_registry.py` | `backends/__init__.py`: the registry, `detect()`, `create()`, `describe()`, the module hooks and class attributes every backend must have, tandemd's `--backend` choices |
| `test_gstreamer.py` | `backends/gstreamer.py`: encoder chains, pipeline descriptions, encoder choice, the shared `missing_pipeline_tools` / `choose_encoder` / `start_encoder` every PipeWire backend uses, `spawn_pipeline` / `stop_pipeline`, and that `gnome.py` still re-exports the moved names |
| `test_layout.py` | `backends/layout.py`: `place_beside`, `valid_scale`, the side phrases in log messages |
| `test_gnome_capture.py` | the GNOME backend's `start_capture` / `stop_capture` command line and process handling |
| `test_core_dispatch.py` | `core.DEPS` rows and the functions that ask the backend's hooks: `touch_setup_check`, `remove_virtual_output`, `primary_monitor_desc` |
| `test_kde_layout.py` | `backends/kde.py`: parsing `kscreen-doctor -j` (fixtures in `fixtures/kde/`) and placement planning, including the left/up shift and the primary fix |
| `test_kde_backend.py` | the KDE backend against a fake helper that speaks the real line protocol: setup, retries, capture, touch lines, teardown |
| `test_kde_kwin_helper.py` | `backends/kwin_helper.py`: paths, the rebuild decision, the desktop entry, and a real build when a compiler and the Wayland headers are installed |
| `test_system.py` | `backends/system.py`: atomic file replacement, the process behind a UNIX socket, a process's command line and environment from a fake `/proc` |
| `test_integration.py` | the TUI and installer texts that depend on the backend: touch setup with and without a runnable fix, the Dependencies notes, the KDE Plasma beta labels, `install.sh` syntax and which release it downloads (`TANDEM_VERSION`) |

When you add a backend, add its module hooks to the registry tests by
registering it (they check every entry in `backends.NAMES`), and add tests for
its parsers and placement planning with sample compositor output in
`tests/fixtures/`.
