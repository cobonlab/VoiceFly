# Voice Fly development roadmap

This is the plan for moving the existing local integration toward independent use by new users. Checked items are implemented in the 0.2.0 source preview; the completion criteria still require the stated real-user checks. Review progress twice weekly; publish a verified update weekly when feasible. Each milestone can take more than one week.

## Public source baseline

- [x] Export the maintained input and speech code without private runtime data or local Git history.
- [x] Retain Squirrel licensing, upstream revisions, and the required librime patch.
- [x] Document the difference between the working local system and this early source release.
- [x] Provide standalone tests, a progress history, contribution guidance, and reproducible upstream source preparation.
- [ ] Verify a complete clean-machine build and publish an integrated demonstration using synthetic text.

## Public defaults and personal learning

- [x] Default to local storage without requiring iCloud.
- [ ] Offer optional synchronization as an explicit choice.
- [x] Separate canonical terms and pronunciations from typing-scheme codes.
- [x] Migrate existing Flypy records with backups and local preservation checks.
- [x] Prepare pinned official Rime resources and generic starter terms with empty personal records.
- [x] Support viewing, editing, deleting, importing, and exporting personal terms.

Completion means that learning works without the maintainer's files or iCloud account, and deleting a term removes its influence from both typing and speech.

## Settings and typing schemes

- [x] Implement a visual settings window that writes effective configuration.
- [x] Offer full pinyin, Flypy, Natural Code and Microsoft double pinyin, Wubi 86, and Wubi-pinyin mixed input with reviewed vocabulary dependencies.
- [ ] Provide an extension path for other Rime schemes.
- [x] Make the Control/Option dictation choice and special space gestures configurable.
- [x] Expose candidate presentation, punctuation, simplified/traditional output, and microphone settings.

Completion means that each advertised scheme has working typing, learning, dictation, switching, and restart behavior; existing personal settings survive upgrades.

## Reliability and recovery

- [ ] Resolve or reproduce outstanding space and arrow-key reports in real applications.
- [ ] Show actionable microphone, model, service, and permissions errors.
- [ ] Validate cancel, application switching, cursor movement, sleep/wake, and Bluetooth device changes.
- [ ] Check password and secure-input boundaries, and remove text-bearing error logs.
- [ ] Verify compatibility in native editors, browsers, Electron applications, messaging, office tools, and terminals.

Completion requires physical-keyboard and real-host checks. Standalone unit tests alone do not establish this milestone.

## Independent installation and releases

- [x] Add combined packaging for input method, ASR service, verified public model resources and login startup.
- [x] Add first-run guidance and service/microphone permission recovery entry points.
- [ ] Verify first-run device testing with an independent new user.
- [ ] Add coordinated updates, rollback, and uninstallation with a personal-data choice.
- [ ] Complete model/dictionary redistribution review, signed artifacts, and Apple notarization.
- [ ] Record supported OS/chip combinations and measured disk and memory use.
- [ ] Have a new user complete installation and daily use without private assistance.

The maintainer verified a separate artwork update without logout on macOS 26.6.2 Apple Silicon, including the native badge, Chinese typing and Control dictation. General full-component upgrades and first installation are not covered by that result. A downloadable consumer installer is not included in the 0.2.0 source preview.

Intel support, more languages, speaker identity filtering, and optional second-model finalization require separate validation before being advertised.
