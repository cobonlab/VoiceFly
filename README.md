# Voice Fly

**Local Chinese-English dictation and Rime keyboard input in a native macOS input method.**

[中文说明](docs/README.zh-CN.md) · [Development and testing](docs/BUILD.md) · [Roadmap](ROADMAP.md) · [Maintenance history](docs/MAINTENANCE.md)

Voice Fly grew from daily use and sustained development with Codex. It combines Squirrel/Rime typing with local streaming speech recognition, so a user can type, hold Control to dictate, see text while speaking, and release to commit. Explicitly saved personal terms can inform both typing and speech.

**Status: 0.2.0-preview.3 — complete installer preview.** A downloadable package now includes the input method, native settings, Python speech runtime and verified local model. It targets **M-series Macs running macOS 26.4+**; Intel and earlier macOS are not supported by this bundle. The installer uses local ad-hoc signing, without a paid Apple Developer ID or Apple notarization. An independent first-install trial is still pending.

## Download and install

[Download the complete Apple Silicon installer](https://github.com/cobonlab/VoiceFly/releases/download/v0.2.0-preview.3/VoiceFly-0.2.0-preview.3-arm64.pkg) · [Release and checksums](https://github.com/cobonlab/VoiceFly/releases/tag/v0.2.0-preview.3) · [Installation steps / 安装说明](docs/INSTALL.md)

You do not need Python, Homebrew, a separate Squirrel installation, an OpenAI API key or an Apple developer membership. macOS may block this non-notarized preview: after trying to open it, check **System Settings → Privacy & Security → Open Anyway**. If updating an existing Voice Fly installation, finish dictation/typing and switch to ABC first. Save your work and log out/in manually after full installation; the installer will not log you out. Approve Voice Fly as an input method and allow Voice Fly Service to use the microphone.

## New in this preview

- Include a complete installable package and model, with corresponding source and checksums.
- Embed the frontend's native dependencies so it can run without Homebrew.
- Stage reviewed public dictionaries and pinned common Rime resources; omit unused optional plugins from the installer.
- Check six schemes with disposable profiles, and verify packaged speech inference and normalization without recording or network access.
- Retain native settings, shared personal vocabulary and the approved framed microphone-and-wind badge.

<img src="docs/images/voicefly-menu-badge.png" width="220" alt="Voice Fly's approved white badge with dark microphone and wind lines on a dark background">

The maintainer previously confirmed frontend build 26's native badge, Chinese typing and Control dictation on macOS 26.6.2 Apple Silicon without logout. This installer was additionally checked after extraction using temporary profiles and restricted processes. Those checks do not replace an independent first installation and real microphone/host check. See the [release notes](docs/releases/0.2.0-preview.3.md).

## Why this project exists

Working with AI involves explaining ideas, requirements, corrections, and context in large amounts of text. For the maintainer, keyboard entry repeatedly interrupted that flow of thought and slowed project work. Voice Fly grew from this daily need: make it easier to express an idea to an AI assistant on a Mac while the idea is still clear.

Three interaction choices shape that goal:

1. **A physical key to speak.** Hold Control to dictate and release to finalize, without repeatedly finding a microphone button.
2. **A short action to save a term.** Double-tap Space, then choose a phrase length with a number key to explicitly save a term. This supports specialist vocabulary and recurring names used in AI conversations.
3. **A shared vocabulary for typing and speech.** Explicitly saved personal terms connect Rime keyboard input and speech hotwords, so a useful term can serve both ways of expressing an idea. Ordinary typing frequency is not automatically treated as permission to change speech recognition.

These are design goals grounded in the maintainer's use, not a measured productivity improvement or a claim that no other input tool offers similar features. The intended ecosystem contribution is a reusable, local integration of Rime and streaming ASR for AI conversations, with shared personal learning and regression coverage for difficult input and audio lifecycle cases.

The speech model runs locally. The baseline recognizer does not require an OpenAI API key. Optional experimental refinement code is present but disabled by default; see [privacy and limitations](docs/PRIVACY.md). Public adoption has not yet been established.

## Implemented in the maintained local system

- Native input-method composition for streaming text and final submission.
- Full pinyin, Flypy, Natural Code and Microsoft double pinyin, Wubi 86, and Wubi-pinyin mixed input.
- Native settings for keyboard schemes, candidate rows, punctuation, simplified/traditional output, dictation key and microphone choice.
- A local shared personal-word ledger with editable pronunciation and JSON import/export.
- Hold-to-talk Control or Option interaction, microphone-ready feedback, and release-to-finalize behavior.
- Refresh of the system-default microphone for each recording, with separate audio capture and decoding.
- Explicit phrase learning with separate ownership for generated dictionaries and runtime personal records.
- Per-recording hotword snapshots, preserving ordinary typing statistics without automatically promoting them into speech corrections.
- Chinese numeric formatting with protection for ordinary expressions and idioms.
- Swift and Python regression tests for learning, input rules, streaming revisions, audio handling, and service state.

The source tree excludes private vocabulary and model weights. The installer is built with an allowlist of public model and starter resources. Validation on the maintainer's Mac and disposable profiles does not establish compatibility on every Mac or in every application.

The project is published as **Voice Fly**. Applications, background services, startup items, and current source modules use this name consistently.

## Repository layout

| Directory | Contents |
| --- | --- |
| `input-method/` | Modified Squirrel frontend, standalone Swift core, and tests |
| `service/` | Local ASR service, audio and text processing, learning support, tests, and optional research tools |
| `patches/` | Required local librime selector changes |
| `scripts/` | Public test runner and pinned upstream source preparation |
| `docs/` | Build boundaries, maintenance evidence, privacy, and Chinese introduction |
| `licenses/` | Retained license texts for pinned upstream dependencies |

`SOURCE_MANIFEST.json` records current upstream revisions and points to the preserved original export hashes. Dependency checkouts are fetched separately rather than bundled. Changes made for this public release are listed in [CHANGELOG.md](CHANGELOG.md).

## Try the reviewable parts

On macOS with Swift installed:

```sh
cd input-method
swift test
```

For the portable Python regression subset, from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r service/requirements-public-tests.txt
.venv/bin/python scripts/test_public.py
```

Neither command records audio, installs the input method, or starts the live service. The full ASR integration tests require additional macOS dependencies; see [BUILD.md](docs/BUILD.md).

## Development plan

The next milestones are independent-user installation trials, broader OS builds, physical-key checks across host applications, and microphone/device recovery. Paid Apple signing/notarization is deferred; the current route is an unsigned installer preview with macOS opening instructions. We aim to review progress twice weekly and publish verified updates weekly when ready. Each update should include a concrete change, validation, and remaining limitations; larger features may span multiple weeks. See [ROADMAP.md](ROADMAP.md).

## Contributing and reporting problems

Use [Issues](https://github.com/cobonlab/VoiceFly/issues) for reproducible bugs and proposals, and read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes. Please use synthetic examples and omit private dictionaries, audio, transcripts, credentials, and identifying logs.

Primary maintainer: Simon Li ([@cobonlab](https://github.com/cobonlab)). The project has been developed with Codex assistance; changes and upstream dependencies remain the maintainer's responsibility. This project is not affiliated with or endorsed by OpenAI.

## License and credits

The Squirrel-derived frontend retains GPLv3. Voice Fly's original code and documentation are offered under GPL-3.0-only; upstream notices and licenses remain intact. External dependencies, models, dictionaries, and artwork retain their own terms. This source release includes no model weights or private dictionary packs. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Thanks to [Squirrel](https://github.com/rime/squirrel), [librime](https://github.com/rime/librime), [plum](https://github.com/rime/plum), [Sparkle](https://github.com/sparkle-project/Sparkle), [X-ASR](https://github.com/Gilgamesh-J/X-ASR), and [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx). Voice Fly contributes their integration and input workflow; it does not claim to have created a new speech recognition model.
