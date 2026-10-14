# Voice Fly changelog

## 0.2.0-preview.5 — 2026-10-14

Automatic recovery for stalled recording finalization and microphone shutdown. Speech service build 23; installer receipt 0.2.0.28; keyboard frontend remains build 27.

- Bound post-release finalization to 6 seconds and microphone cleanup to 3 seconds; timeout exits the stuck process for launchd recovery.
- Deliver finalized text before microphone cleanup, while refusing a new recording until the previous device is released.
- Preserve errors from native stop/close; never mark a failed cleanup as idle or overlap it with a new session.
- Keep deadlines session-local, cancel successful completion and protect process recovery from stalled error notification/diagnostic writes.
- Add privacy-limited recovery records and health stage/timing; no audio or recognized text is persisted.
- Verify 180 focused service/audio/text tests, 153 portable tests (152 passed, one skipped), 63 Swift tests, packaged model/normalization diagnostics and isolated launchd fault recovery with a new PID.
- Installed the tested service on the maintainer's Mac with the previous service backed up. The maintainer confirmed two consecutive Control-held dictations both committed normally.

Underlying CoreAudio/device faults may still occur; recovery takes several seconds, and an utterance that times out before finalization may need to be repeated. Independent full installation and wider device coverage remain pending. See [release notes](docs/releases/0.2.0-preview.5.md).

## 0.2.0-preview.4 — 2026-10-13

Keyboard reliability maintenance release, frontend build 27.

- Treat held Space as ordinary whitespace rather than repeated double/triple-tap commands; flush withheld physical taps once and pass following repeats to the host.
- Submit deferred and fallback spaces through a marked-text commit so text clients receive a complete composition lifecycle.
- Pass Left/Right directly to the host when there is no unfinished composition, avoiding unnecessary Rime/marked-text/caret-rectangle updates.
- Reset cached caret and selection after commits and input-source activation.
- Run installer user commands in the logged-in GUI bootstrap environment so ABC/idle checks read the desktop's current input source instead of stale installer-context state.
- Add three held-Space regressions: 63 Swift tests passed; native Release build and strict bundle signatures passed.
- Installed build 27 on the maintainer's Mac with a recoverable build-26 backup; the speech service process was preserved. Reports concern Codex, Telegram, WeChat and web inputs; resolution in every host is still awaiting real-use confirmation.

Double/triple-Space gestures are retained, including a wait of up to 0.55 seconds for a single plain Space when phrase learning is enabled. This release does not claim that all reported host issues are resolved. Full-installer independent-user validation remains pending.

## 0.2.0-preview.3 — 2026-10-01

Complete Apple Silicon installer preview with corresponding source. No paid Developer ID signature or Apple notarization.

- Include input method, native settings, speech runtime and verified public model in one package.
- Embed glog/gflags and rewrite native dependency paths inside the staged frontend; eliminate dependence on the maintainer's Homebrew installation.
- Stage the reviewed public schemes and pinned prelude/essay/stroke resources; omit unrelated optional plugin binaries.
- Retain bundled dependency/model licenses and remove local pip acquisition metadata.
- Add a packaged diagnostic that checks five model hashes, silence inference, 24 text-normalization cases and seven synthetic personal-phonetic cases without opening a microphone or starting the service.
- Guard existing-install upgrades against active recording and wait for frontend exit; replace whole bundles and preserve private backups for recovery.
- Keep logout manual: explain first-install/full-upgrade logout in the installer, without automatically closing user applications.
- Verify extracted bundles, native dependencies, architecture, signatures, isolated six-scheme candidates and offline speech diagnostics. Independent first-install validation remains pending.

## 0.2.0-preview.2 — 2026-10-01

Public source preview. No application installer or model weights are attached.

### Settings and shared input

- Add native settings, first-use guidance, service status and microphone recovery entry points.
- Offer full pinyin, Flypy, Natural Code and Microsoft double pinyin, Wubi 86 and Wubi-pinyin mixed input from pinned official Rime sources, with their licenses and authors retained.
- Default personal words to local storage, migrate earlier records with backups, and add pronunciation editing and JSON import/export.
- Make dictation key, microphone preference, candidate rows, punctuation and output options configurable.
- Defer configuration changes until current input ends rather than interrupting dictation.

### Appearance and update reliability

- Unify active applications, services, startup items and source modules under Voice Fly. Retired identifiers remain for migration and provenance only.
- Open settings without a Dock or application-switcher icon, and exit after the last settings window closes.
- Adopt the original outlined microphone and curved wind mark, with a framed native input-source badge.
- Correct the blank white badge by using vector alpha cutouts that survive native monochrome tinting.
- Add a guarded existing-install artwork update: wait for idle speech and ABC selection, gracefully exit the frontend, preserve whole old bundles in a private backup, verify signatures and register the canonical installed path.
- Refresh the input-menu display without restarting speech or logging out. Preserve existing input-source approval; leave incoming selection to the native menu.
- Add a read-only connection check requiring a responding live input controller, client and Rime session.

### Build and verification

- Add public model download checksums, allowlisted service packaging, combined installation tooling and an archiving uninstaller.
- Fix schema deployment with paths containing spaces/non-ASCII characters and restrict new defaults to bundled schemes.
- Re-run 144 portable Python tests (143 passed, one skipped) and 60 Swift tests (all passed) from the current public source snapshot.
- The maintainer confirmed final frontend build 26's native badge, Chinese typing and Control dictation on macOS 26.6.2 Apple Silicon without logout. Six Rime schemes were checked with disposable profiles.

The local full bundle targets Apple Silicon/macOS 26.4+, uses development signing and has no Apple notarization. First installation, general full-component hot upgrades, a fresh development-machine build and independent-user coverage remain work. The source preview is not a consumer installer. See [release notes](docs/releases/0.2.0-preview.2.md).

## 0.1.0-preview.1 — 2026-09-30

- Exported the maintained input-method integration, Swift core, Python service and public regression tests.
- Retained the upstream revisions, original frontend license, dependency license notices and librime selector patch.
- Excluded private dictionaries, runtime records, audio, models, application binaries, historic Git metadata and four private-fixture-dependent test modules.
- Added bilingual project documentation, contribution guidance, maintenance history, staged roadmap, public test runner, and pinned upstream preparation.
- Adopted Voice Fly as the public project name and VoiceFly as the repository name.
- Added a public Python/Swift CI workflow; CI results are recorded by GitHub when it runs.
- Added an upstream README notice and changed the frontend Makefile to use the explicit pinned Sparkle preparation instead of assuming a submodule in this standalone export.
- Classified this as early development source with no new-user installation package. Visual settings, other integrated typing schemes, public defaults and combined installation remain planned.

Pre-publication local development is summarized in `docs/MAINTENANCE.md`. It is not represented as public release history.
