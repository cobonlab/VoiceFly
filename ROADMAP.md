# Voice Fly development roadmap

This is the plan for moving the existing local integration toward independent use by new users. Items below are planned work, not completed functionality. Review progress twice weekly; publish a verified update weekly when feasible. Each milestone can take more than one week.

## Public source baseline

- [x] Export the maintained input and speech code without private runtime data or local Git history.
- [x] Retain Squirrel licensing, upstream revisions, and the required librime patch.
- [x] Document the difference between the working local system and this early source release.
- [x] Provide standalone tests, a progress history, contribution guidance, and reproducible upstream source preparation.
- [ ] Verify a complete clean-machine build and publish an integrated demonstration using synthetic text.

## Public defaults and personal learning

- [ ] Default to local storage; make synchronization an explicit choice.
- [ ] Separate canonical terms and pronunciations from typing-scheme codes.
- [ ] Migrate existing Flypy records without losing terms or corrupting order.
- [ ] Prepare a licensed public vocabulary profile with empty personal records.
- [ ] Support viewing, editing, deleting, importing, and exporting personal terms.

Completion means that learning works without the maintainer's files or iCloud account, and deleting a term removes its influence from both typing and speech.

## Settings and typing schemes

- [ ] Implement a visual settings window that writes effective configuration.
- [ ] Offer full pinyin, Flypy, Natural Code and Microsoft double pinyin, Wubi 86, and Wubi-pinyin mixed input with reviewed vocabulary dependencies.
- [ ] Provide an extension path for other Rime schemes.
- [ ] Make dictation shortcuts and special space gestures configurable.
- [ ] Expose candidate presentation, punctuation, simplified/traditional output, and microphone settings.

Completion means that each advertised scheme has working typing, learning, dictation, switching, and restart behavior; existing personal settings survive upgrades.

## Reliability and recovery

- [ ] Resolve or reproduce outstanding space and arrow-key reports in real applications.
- [ ] Show actionable microphone, model, service, and permissions errors.
- [ ] Validate cancel, application switching, cursor movement, sleep/wake, and Bluetooth device changes.
- [ ] Check password and secure-input boundaries, and remove text-bearing error logs.
- [ ] Verify compatibility in native editors, browsers, Electron applications, messaging, office tools, and terminals.

Completion requires physical-keyboard and real-host checks. Standalone unit tests alone do not establish this milestone.

## Independent installation and releases

- [ ] Package input method, ASR service, models or verified model acquisition, and login startup together.
- [ ] Add first-run setup, device testing, and permission recovery.
- [ ] Add coordinated updates, rollback, and uninstallation with a personal-data choice.
- [ ] Complete model/dictionary redistribution review, signed artifacts, and Apple notarization.
- [ ] Record supported OS/chip combinations and measured disk and memory use.
- [ ] Have a new user complete installation and daily use without private assistance.

Intel support, more languages, speaker identity filtering, and optional second-model finalization require separate validation before being advertised.
