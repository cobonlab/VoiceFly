# Voice Fly

**Local Chinese-English dictation and Rime keyboard input in a native macOS input method.**

[中文说明](docs/README.zh-CN.md) · [Development and testing](docs/BUILD.md) · [Roadmap](ROADMAP.md) · [Maintenance history](docs/MAINTENANCE.md)

Voice Fly grew from daily use and sustained development with Codex. It combines Squirrel/Rime typing with local streaming speech recognition, so a user can type, hold Control to dictate, see text while speaking, and release to commit. Explicitly saved personal terms can inform both typing and speech.

**Status: early development source release.** The integrated system runs on the maintainer's Apple Silicon Mac. This repository makes the integration and regression tests available for review and contribution. A complete installer for new users, a visual settings window, and integrated support for additional typing schemes are still being developed. There is no consumer installation package in this release.

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
- Rime/Flypy typing with a custom candidate grid.
- Hold-to-talk Control interaction, microphone-ready feedback, and release-to-finalize behavior.
- Refresh of the system-default microphone for each recording, with separate audio capture and decoding.
- Explicit phrase learning with separate ownership for generated dictionaries and runtime personal records.
- Per-recording hotword snapshots, preserving ordinary typing statistics without automatically promoting them into speech corrections.
- Chinese numeric formatting with protection for ordinary expressions and idioms.
- Swift and Python regression tests for learning, input rules, streaming revisions, audio handling, and service state.

These describe the maintainer's integrated local system. The source release excludes private vocabulary and external models; it is not a claim that a new user can install the complete system with one click today.

The project is published as **Voice Fly**. Some source filenames, Swift symbols, and local component identifiers retain the earlier `VoiceFlypy` name for compatibility with the existing development system.

## Repository layout

| Directory | Contents |
| --- | --- |
| `input-method/` | Modified Squirrel frontend, standalone Swift core, and tests |
| `service/` | Local ASR service, audio and text processing, learning support, tests, and optional research tools |
| `patches/` | Required local librime selector changes |
| `scripts/` | Public test runner and pinned upstream source preparation |
| `docs/` | Build boundaries, maintenance evidence, privacy, and Chinese introduction |
| `licenses/` | Retained license texts for pinned upstream dependencies |

`SOURCE_MANIFEST.json` records the original exported source hashes and upstream revisions. Dependency checkouts are fetched separately rather than bundled. Changes made for this public release are listed in [CHANGELOG.md](CHANGELOG.md).

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

The next milestones are public defaults and scheme-independent learning, visual settings and full-pinyin/Wubi choices, actionable error recovery, and a combined installer with upgrade and removal support. We aim to review progress twice weekly and publish verified updates weekly when ready. Each update should include a concrete change, validation, and remaining limitations; larger features may span multiple weeks. See [ROADMAP.md](ROADMAP.md).

## Contributing and reporting problems

Use [Issues](https://github.com/cobonlab/VoiceFly/issues) for reproducible bugs and proposals, and read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes. Please use synthetic examples and omit private dictionaries, audio, transcripts, credentials, and identifying logs.

Primary maintainer: Simon Li ([@cobonlab](https://github.com/cobonlab)). The project has been developed with Codex assistance; changes and upstream dependencies remain the maintainer's responsibility. This project is not affiliated with or endorsed by OpenAI.

## License and credits

The Squirrel-derived frontend retains GPLv3. Voice Fly's original code and documentation are offered under GPL-3.0-only; upstream notices and licenses remain intact. External dependencies, models, dictionaries, and artwork retain their own terms. This source release includes no model weights or private dictionary packs. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Thanks to [Squirrel](https://github.com/rime/squirrel), [librime](https://github.com/rime/librime), [plum](https://github.com/rime/plum), [Sparkle](https://github.com/sparkle-project/Sparkle), [X-ASR](https://github.com/Gilgamesh-J/X-ASR), and [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx). Voice Fly contributes their integration and input workflow; it does not claim to have created a new speech recognition model.
