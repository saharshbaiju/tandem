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
  through `SO_PEERCRED`;
* the real libxkbcommon, through `ctypes`, for the keyboard layout lookups
  (skipped where it isn't installed).

There are 355 tests.

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
| `test_encoder_fallback.py` | issue #6: reading enum values and integer ranges from gst-inspect, settings checked before gst-launch sees them, the VA rate control choice (`cbr`, `vbr`, or unusable), the order encoders are tried in, `EncoderSelector` falling back and remembering, an encoder that only rejects the BT.709 colour caps being kept and retried without them, `ended_with_an_error` against real processes, and `about.VERSION` matching the Android `versionName` |
| `test_colour.py` | issue #7, `colour.py`: the wf-recorder colour arguments for CPU, VA-API and NVENC encoders, an old `scale_vaapi` left alone rather than mislabelled, no ffmpeg binary, the GStreamer caps (VA memory caps included, and the unpinned chain as before 0.9.0), and Hyprland retrying an encoder without the colour settings before moving on to the next one |
| `test_keyboard.py` | typing: `KeyCodeTyper` (Shift added around capitals, Ctrl shortcuts, unknown keysyms skipped, stray releases, letters released before Ctrl), `Keymap` against the real libxkbcommon (US layout, AltGr and Unicode keysyms, an unknown layout), `KeyRouter` releasing held keys on disconnect, `PhoneInput` with `KEYS` frames and the `keys` field of `INPT`, the Plasma `kxkbrc` and Hyprland `kb_*` layout readers, and `KwinKeyboard` sending keysyms on new KWin, key codes on older KWin, and no keyboard on the oldest |
| `test_pointer.py` | pointer mode: `PointerRouter` (a move before every press, a drag let go on disconnect, unknown buttons, stray releases), `PhoneInput` with `PNTR` frames and the `pointer` field of `INPT`, malformed frames ignored, Hyprland's `layout_geometry`, the uinput pointer turning a fraction of the phone's monitor into a layout position, and `KwinPointer`'s helper commands (and no pointer on old KWin) |
| `test_update_check.py` | the "newer release is out" check: version parsing and numeric comparison, the once-a-day cache, keeping the last answer when offline, a generic User-Agent, odd GitHub answers ignored, and `TANDEM_NO_UPDATE_CHECK` |
| `test_device_fit.py` | fit to device: reading `SIZE` frames (and their `now` flag), a phone turned to portrait getting a tall monitor, a turn applied without the settle wait, the banner's `fit` field, the size caps and rounding, the remembered size, one resize per burst of reports and none with two phones, `resize_monitor` putting the old size back when a backend fails, the resize running between stopping and starting the encoder, each backend's `resize()` and touch retargeting, and the *Fit to device* setting in the TUI |
| `test_tui_log_and_encoder.py` | the Encoder setting reaching the daemon, the daemon log file (`~/.local/state/tandem/tandemd.log`, one per TUI session), and the live view wrapping log lines instead of cutting them off |
| `test_layout.py` | `backends/layout.py`: `place_beside`, `valid_scale`, the side phrases in log messages |
| `test_gnome_capture.py` | the GNOME backend's `start_capture` / `stop_capture` command line and process handling |
| `test_core_dispatch.py` | `core.DEPS` rows and the functions that ask the backend's hooks: `touch_setup_check`, `remove_virtual_output`, `primary_monitor_desc` |
| `test_kde_layout.py` | `backends/kde.py`: parsing `kscreen-doctor -j` (fixtures in `fixtures/kde/`) and placement planning, including the left/up shift and the primary fix |
| `test_kde_backend.py` | the KDE backend against a fake helper that speaks the real line protocol: setup, retries, capture, touch lines, teardown |
| `test_kde_kwin_helper.py` | `backends/kwin_helper.py`: paths, the rebuild decision, the desktop entry, and a real build when a compiler and the Wayland headers are installed |
| `test_system.py` | `backends/system.py`: atomic file replacement, the process behind a UNIX socket, a process's command line and environment from a fake `/proc` |
| `test_integration.py` | the TUI and installer texts that depend on the backend: touch setup with and without a runnable fix, the Dependencies notes, no backend labelled beta, `install.sh` syntax and which release it downloads (`TANDEM_VERSION`) |

## App tests

The Android app has JVM unit tests in `android/app/src/test/` (no phone or
emulator needed):

```bash
cd android && JAVA_HOME=/opt/android-studio/jbr ./gradlew testDebugUnitTest
```

| Class | Covers |
|---|---|
| `UpdateCheckTest` | version parsing and ordering, betas, and which release the update card offers |
| `KeysymsTest` | characters to keysyms (Latin-1 is its own keysym, the rest Unicode), the `KEYS` body, and `TextToKeys`: plain commits, composing words sending only the difference, autocorrect replacing only what changed, a finished word not retyped, deletes shortening the composing word, an emoji as one Backspace |
| `OrientationWatcherTest` | the phone's turn from the screen as drawn: held as drawn is no turn, a landscape app held upright and a portrait app on its side are quarter turns, the snap angle, and wrapping around 360 degrees |

When you add a backend, add its module hooks to the registry tests by
registering it (they check every entry in `backends.NAMES`), and add tests for
its parsers and placement planning with sample compositor output in
`tests/fixtures/`.
