# Build and install the 0.2.0 source preview

The public `0.2.0-preview.2` release contains source and public resources, **not an application installer**. Developers can follow [BUILD.md](BUILD.md) to prepare a local development package. The steps below describe that package's installation flow; they are not a promise that a new user can install this release without development tools.

This preview is for **Apple Silicon Macs running macOS 26.4 or later**. The bundled speech libraries require that version. Intel and earlier macOS versions are not advertised as supported.

1. Build `VoiceFly-0.2.0-preview.2-arm64.pkg` using the documented source preparation and packaging steps. Keep the package and its checksum together. Do not substitute the maintainer's installed applications or private backups.
2. Open the installer and complete macOS administrator verification. It installs the input method, settings window, public speech model and login service together.
3. **Save your work, log out of your Mac account from the Apple menu, and log back in.** Installing or renaming an input method requires a new macOS user session.
4. Open **Voice Fly** in Applications. Choose a keyboard scheme and use the first-use tab to prepare and enable the input method. Approve macOS's request to enable **Voice Fly**. Then select Voice Fly using the native input-source menu. Until approved, its row may appear grayed out and another input method may remain selected.
5. Hold Control to speak. Allow **Voice Fly Service** to use the microphone when macOS asks. The first launch can spend about a minute preparing local text normalization resources; the status tab shows loading.
6. Try typing and dictation in a normal text field. Password fields are excluded from voice capture and personal learning when macOS reports secure input.

The preview uses local development signing. It does not yet have an Apple Developer ID signature or Apple notarization. macOS may block a downloaded installer. Follow macOS's own review/open instructions only after checking that it came from this repository; do not disable system security protections.

## Installed components

| Component | Location |
| --- | --- |
| Input method | `/Library/Input Methods/VoiceFly.app` |
| Settings | `/Applications/Voice Fly.app` |
| Speech service | `/Applications/Voice Fly Service.app` |
| Login startup | `~/Library/LaunchAgents/com.simonli.voicefly.service.plist` |
| Personal words and settings | `~/Library/Application Support/VoiceFly/` |
| Rime configuration | `~/Library/Rime-VoiceFly/` |

Updates preserve personal dictionaries. The installer makes a private backup before switching components and attempts recovery if configuration deployment fails. Earlier product names are recognized only for migration; active components use Voice Fly. Review backups locally and never upload them as issue attachments.

The existing-install artwork update has a separate no-logout path (`scripts/build_installer.py --hot-appearance`). Finish the current input/dictation and temporarily choose ABC before installing. Only Voice Fly is restarted; choose it again through the native menu afterward. The maintainer confirmed the new icon, Chinese typing and dictation on macOS 26.6.2 Apple Silicon without closing other applications. First installation and general full-component upgrades retain the logout flow until independently validated; do not claim that all upgrades are already seamless.

Programmatic selection is not a working-input check: on macOS 26 it can return success while leaving the input controller inactive ([upstream report](https://github.com/rime/squirrel/issues/1162)). Validate native switching, actual Chinese typing, and dictation before accepting an update. Configuration and personal vocabulary changes already use deferred reload rather than replacing the input-method application.

## Remove the application

From a source checkout, run `sudo bash scripts/uninstall.sh`. The script disables the input method and login service, then archives applications in the user's backup folder. Personal words and settings remain available for recovery. The preview targets one logged-in user; managed multi-user installation still needs separate validation.

## Troubleshooting

Open Voice Fly's status/help tab to restart the speech service. Check the microphone permission in System Settings if typing works but speech does not. If the input source is missing, use the first-use activation button or add Voice Fly in macOS keyboard/input-source settings. Report the OS version, chip and reproducible steps using invented text; omit personal words, audio and raw logs.
