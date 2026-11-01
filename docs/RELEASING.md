# Tandem distribution and releases

## How users get Tandem

| Piece | Channel | What the user does |
| --- | --- | --- |
| PC side (Python, no build step) | `install.sh` from the `main` branch. It downloads the latest release's `tandem-linux.tar.gz`, or the `main` tarball if no release exists yet | `curl -fsSL https://raw.githubusercontent.com/saharshbaiju/tandem/main/install.sh \| bash`, then `tandem` |
| Phone app | GitHub Releases: `Tandem-<version>.apk`, plus the same file as `Tandem.apk`, whose name never changes. The website's Download buttons open the release page, where the file is tapped. Chrome on Android can leave an APK fetched directly through GitHub's redirects stuck at 100 %. The APK is signed with the Tandem release key | Download on the phone, then install |
| Debug app (for testers) | GitHub Releases: `Tandem-<version>-debug.apk`. Package `io.github.saharshbaiju.tandem.debug`, named *Tandem (debug)* | Installs next to the normal app. Used for logs and testing |

The installer does not depend on the distribution. It asks `linux/core.py` which
packages are missing for this distribution and desktop, using the same list as the
TUI's Dependencies screen, and installs them with apt, dnf or pacman. Nothing is
compiled, so the same tarball works on Ubuntu, Fedora and Arch.

Verified: `install.sh` on Ubuntu 24.04 in a fresh container installs the packages
and the `tandem` command, which resolves in a new shell. The GNOME backend streams
on Ubuntu 24.04 (Mutter 46, x264enc) and on Fedora 42 (Mutter 48, openh264enc).

## Signing key: keep it safe

Android installs an update only if it is signed with the **same key** as the app
already installed. The release key lives outside the repository:

```
~/.config/amvd-release/amvd-release.jks          keystore (alias "amvd"; the file name predates the rename)
~/.gradle/gradle.properties                      TANDEM_KEYSTORE / TANDEM_KEYSTORE_PASSWORD / TANDEM_KEY_ALIAS
```

**Back up both** in a password manager or on an encrypted drive. If they are lost,
every user must uninstall the app before the next version can be installed. Never
commit them. Without these properties, `assembleRelease` falls back to the debug
key. That is fine for local testing, but such a build must not be published.

The build reads the `TANDEM_*` names. It also accepts the older `ASEXT_*` names as
a fallback (see `android/app/build.gradle.kts`).

Debug builds use their own package ID (`applicationIdSuffix ".debug"`) and the name
*Tandem (debug)*. A debug build and the release app install side by side, and they
never replace each other.

## Names

The project was called amVD up to version 0.4.x. Its app was called Screen Extender,
with the package `com.archextender`, and its command was `amvd`. Since 0.5, every
name is **Tandem**:

| What | Current name | Notes for users upgrading from 0.4.x |
| --- | --- | --- |
| Android package | `io.github.saharshbaiju.tandem` | Installs as a new app next to Screen Extender. Uninstall the old app by hand. Pairing is needed again in any case |
| QR code and deep link | `tandem://` | An old `asext://` QR code scanned in Tandem asks for the computer to be updated |
| Command and install directory | `tandem`, `~/.local/share/tandem` | `install.sh` removes the old `amvd` install |
| Config directory | `~/.config/tandem` | Moved on first run. Settings, pairing key and TLS certificate are kept |
| System files | `70-tandem-uinput.rules`, `tandem-uinput.conf`, `90-tandem-usb-tether.network` | Older `amvd` system files keep working. Nothing to do |
| Release assets | `Tandem-<v>.apk`, `tandem-linux.tar.gz` | Older releases used `amVD-<v>.apk` and `amvd-linux.tar.gz` |
| GitHub repository | `saharshbaiju/tandem` | Renamed on GitHub. Releases, tags and stars were kept, and old URLs redirect |

The 0.5.0 release notes describe the rename.

## Cutting a release

1. Increase `versionCode` by one, and update `versionName`, in
   `android/app/build.gradle.kts`.
2. Build and package:

   ```bash
   V=0.6.1
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

`install.sh` always fetches `releases/latest/download/tandem-linux.tar.gz`. A new
release is therefore picked up by running the install command again.

## Planned distribution channels

- An **AUR** package (`tandem-git`) for Arch.
- **F-Droid** for the app. It needs a reproducible build without the Play services
  code scanner, or a flavour that leaves the scanner out.
- **Google Play**, on the internal testing track, once the app is stable.
- A `.deb` or COPR repository, for users who want updates through their package
  manager.
