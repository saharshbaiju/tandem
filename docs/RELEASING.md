# Tandem — Distribution & releasing

## How users get it

| Piece | Channel | What the user does |
| --- | --- | --- |
| PC side (Python, no build step) | `install.sh` from the `main` branch; downloads the latest release's `tandem-linux.tar.gz` (falls back to the `main` tarball if there's no release yet) | `curl -fsSL https://raw.githubusercontent.com/saharshbaiju/tandem/main/install.sh \| bash`, then `tandem` |
| Phone app | GitHub Releases: `Tandem-<version>.apk`, plus the same file as `Tandem.apk`, a name that never changes (the website's Download buttons open the release page, where people tap it: Chrome on Android can leave an APK fetched straight through GitHub's redirects stuck at 100%); signed with the Tandem release key | Download on the phone, install |
| Debug app (testers) | GitHub Releases: `Tandem-<version>-debug.apk` — package `io.github.saharshbaiju.tandem.debug`, named *Tandem (debug)* | Installs *next to* the normal app; for logs and testing |

The installer is distro-agnostic: it asks `linux/core.py` which packages are
missing for *this* distro and desktop (the same list as the TUI's Dependencies
screen) and installs them with apt / dnf / pacman. Nothing is compiled, so the
same tarball works on Ubuntu, Fedora and Arch.

Verified: `install.sh` on Ubuntu 24.04 (fresh container) installs the
packages and the `tandem` command, which then resolves in a new shell. The GNOME
backend streams on Ubuntu 24.04 (Mutter 46, x264enc) and Fedora 42 (Mutter 48,
openh264enc).

## Signing key — keep it safe

Android only installs an update if it's signed with the **same key** as the
installed app. The release key lives outside the repo:

```
~/.config/amvd-release/amvd-release.jks          keystore (alias "amvd", kept from before the rename)
~/.gradle/gradle.properties                      TANDEM_KEYSTORE / TANDEM_KEYSTORE_PASSWORD / TANDEM_KEY_ALIAS
```

**Back up both** (password manager / encrypted drive). Losing them means every
user has to uninstall before the next version installs. Never commit them.
Without these properties `assembleRelease` falls back to the debug key, which is
fine for local testing but must not be published.

Debug builds use their own package id (`applicationIdSuffix ".debug"`) and the
name *Tandem (debug)*, so a debug build and the release app install side
by side and never replace each other.

## Renamed from amVD / Screen Extender (0.5)

Up to 0.4.x the project was *amVD*, the app *Screen Extender* (package
`com.archextender`) and the command `amvd`. From 0.5 everything is **Tandem**:

| What | Before | Now | What existing users notice |
| --- | --- | --- | --- |
| Android package | `com.archextender` | `io.github.saharshbaiju.tandem` | Tandem installs as a **new app** next to Screen Extender; uninstall the old one by hand. (0.5 needs pairing again anyway.) |
| QR / deep link | `asext://` | `tandem://` | An old `asext://` QR scanned in Tandem says the computer needs updating |
| Command, install dir | `amvd`, `~/.local/share/amvd` | `tandem`, `~/.local/share/tandem` | `install.sh` removes the old `amvd` install |
| Config dir | `~/.config/archscreenextender` | `~/.config/tandem` | Moved on first run (settings, pairing key, TLS certificate kept) |
| System files | `70-amvd-uinput.rules`, `amvd-uinput.conf`, `90-amvd-usb-tether.network` | `…tandem…` | Old ones keep working; nothing to do |
| Release assets | `amVD-<v>.apk`, `amvd-linux.tar.gz` | `Tandem-<v>.apk`, `tandem-linux.tar.gz` | — |
| GitHub repo | `saharshbaiju/amVD` | `saharshbaiju/tandem` | Renamed on GitHub (releases, tags and stars kept); old URLs redirect |
| Signing properties | `ASEXT_*` | `TANDEM_*` | The build still reads `ASEXT_*`; the keystore and its alias `amvd` are unchanged |

The 0.5.0 release notes say so.

## Cutting a release

1. Bump `versionCode` (+1) and `versionName` in `android/app/build.gradle.kts`.
2. Build and package:

   ```bash
   V=0.5.1
   (cd android && JAVA_HOME=/opt/android-studio/jbr ./gradlew assembleRelease assembleDebug)
   mkdir -p dist
   cp android/app/build/outputs/apk/release/app-release.apk dist/Tandem-$V.apk
   cp dist/Tandem-$V.apk dist/Tandem.apk          # the same app under a name that never changes
   cp android/app/build/outputs/apk/debug/app-debug.apk dist/Tandem-$V-debug.apk
   tar --exclude=__pycache__ -czf dist/tandem-linux.tar.gz linux LICENSE NOTICE
   (cd dist && sha256sum Tandem-$V.apk Tandem.apk Tandem-$V-debug.apk tandem-linux.tar.gz > SHA256SUMS)
   ```

3. Commit, tag and publish:

   ```bash
   git tag -a v$V -m "Tandem $V" && git push origin main v$V
   gh release create v$V dist/Tandem-$V.apk dist/Tandem.apk dist/Tandem-$V-debug.apk \
       dist/tandem-linux.tar.gz dist/SHA256SUMS \
       --title "Tandem $V" --notes-file <notes>
   ```

`install.sh` always fetches `releases/latest/download/tandem-linux.tar.gz`, so a
new release is picked up by re-running the install line.

## Possible next channels

- **AUR** package (`tandem-git`) for Arch.
- **F-Droid** for the app (needs a reproducible build without Play-services
  code scanner, or a flavor that drops it).
- **Google Play** (internal testing track) once the app is stable.
- A `.deb` / COPR repo if users want updates through their package manager.
