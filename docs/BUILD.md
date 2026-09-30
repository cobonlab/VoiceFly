# Development and testing

This repository is an early source release. Standalone tests are the most direct reproducible entry point. The full input-method/service installer is not ready for independent installation; avoid treating an upstream Squirrel build guide as a complete Voice Fly installation guide.

## Swift core

On macOS with Swift 5.10 or newer:

```sh
cd input-method
swift test
```

This builds only the standalone learning and interaction core. It does not compile the frontend, install an input source, or verify InputMethodKit in real applications.

## Portable Python tests

Use Python 3.13 for the documented environment. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r service/requirements-public-tests.txt
.venv/bin/python scripts/test_public.py
```

The test runner uses an explicit public subset. Tests that depended on the maintainer's vocabulary packs and production profiles are omitted from this source release. Additional experimental tests remain available but are not silently claimed as CI coverage.

## Full speech integration tests

`service/requirements-runtime-reference.txt` lists direct versions observed in the working local environment. It is a reference, not a verified dependency lock. In particular, `wetextprocessing==1.1.1.dev1` was locally installed; clean acquisition of that exact development version and a portable Pynini build still need verification. Do not substitute a different normalization library and claim the same behavior without running the integration tests.

In an environment that already contains the listed dependencies, from `service/`:

```sh
python -m unittest test_voice_audio test_voice_hotwords test_voice_personal_phonetics test_voice_service_async_reload test_voice_streaming_protocol test_voice_refiner test_itn_v2 test_voice_tool_rules
```

These tests do not start the real recording service. `voiceflypy_smoke.py` does start a real recording against an installed service and prints recognized text, so it is a separate explicit manual diagnostic, not a CI test.

## Pinned input-method dependencies

```sh
python3 scripts/prepare_upstreams.py
```

The script clones the official librime, Sparkle, and plum repositories at the commits in the manifest and applies `patches/librime.patch`. It refuses to replace an existing checkout with modifications. Dependency folders are ignored by the public repository. Their own nested dependencies can be initialized by passing `--recursive`; downloaded code retains its own licenses.

The original Makefile and Xcode project are included as implementation evidence. For an exploratory frontend build, the required stages are:

1. Prepare the pinned checkouts and librime patch with the script above.
2. Review the included upstream `input-method/INSTALL.md` and install the Xcode/CMake/Boost dependencies needed for source building.
3. From `input-method/`, run the librime, data, and Sparkle build targets, then the Release target with `ARCHS=arm64` and local development signing.
4. Verify public schema resources, dictionary sources, and fonts/icons needed by the resulting bundle before installing it.

The archived `action-install.sh` obtains upstream prebuilt binaries. Those binaries do not include this repository's librime selector patch, so that shortcut cannot demonstrate the modified candidate behavior. Do not overwrite the patched library with an upstream binary while claiming that it contains the patch.

A complete clean frontend build, public Rime defaults, pronunciation/decomposition data, ASR packaging, and the combined installer remain milestone work. This document intentionally makes no one-command full-application build promise.

## Models and service launch

The maintained baseline uses the X-ASR Chinese-English 480 ms streaming int8 export dated 2026-06-05, with `tokens.txt`, `encoder.int8.onnx`, `decoder.onnx`, and `joiner.int8.onnx`. The source service looks for that model directory alongside the Python modules; a bundled service uses `models/streaming-zipformer`.

No model weights are redistributed here. Obtain deployment artifacts from [X-ASR](https://github.com/Gilgamesh-J/X-ASR) and follow their terms; exact public acquisition, hashes, tokenizer resources, and model-license verification are still to be finalized for our installer. Generic repository licensing must not be assumed to license every downloadable weight file.

`service/com.simonli.voiceflypy.service.plist` is a launch template, not ready to load: `__HOME__` must be replaced with the target user's actual home directory. The current source still has local-user/iCloud assumptions documented in the roadmap. Do not enable login startup until those paths and installed assets have been deliberately configured.

## Verification policy

Synthetic unit tests, build verification, real keyboard/host checks, microphone/device checks, and clean installation answer different questions. Record the actual environment and outcome for each relevant stage. Never attach the maintainer's application, model, recovery archive, private ledger, or local environment as a substitute for a public build.
