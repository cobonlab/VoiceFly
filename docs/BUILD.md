# Development and testing

Standalone tests are the most direct reproducible entry point. The 0.2.0 preview includes native settings and combined packaging. The current speech bundle requires Apple Silicon/macOS 26.4+; a complete build from a fresh development machine remains unverified.

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

`service/requirements-runtime-reference.txt` lists direct versions observed in the working local environment. It is a reference, not a verified dependency lock. The installed WeTextProcessing tn/itn source was compared with official revision `039007e6b819bd40c4c4a0644f3939da1da47e44`: all 441 source/data files match apart from the generated version file. That revision is now pinned instead of an unavailable development package version. Native Pynini/OpenFST builds and dependency acquisition on a fresh machine still need validation. Do not substitute a different normalization library and claim the same behavior without running the integration tests.

In an environment that already contains the listed dependencies, from `service/`:

```sh
python -m unittest test_voice_audio test_voice_hotwords test_voice_personal_phonetics test_voice_service_async_reload test_voice_streaming_protocol test_voice_refiner test_itn_v2 test_voice_tool_rules
```

These tests do not start the real recording service. `voicefly_smoke.py` does start a real recording against an installed service and prints recognized text, so it is a separate explicit manual diagnostic, not a CI test.

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

The current frontend and public model bundle were built locally, and actual Rime translation was exercised in six schemes with disposable profiles. Public scheme sources and their commits/licenses are listed in `third-party/rime-schemes/SOURCES.json`. Regenerate resources with `python3 scripts/prepare_schemas.py /path/to/pinned-rime-checkouts`, then copy the files in `input-method/data/voicefly/` to the ignored `input-method/data/plum/` before building. A full build on a fresh development machine remains unverified.

## Models and service launch

The maintained baseline uses the X-ASR Chinese-English 480 ms streaming int8 export dated 2026-06-05, with `tokens.txt`, `encoder.int8.onnx`, `decoder.onnx`, and `joiner.int8.onnx`. The source service looks for that model directory alongside the Python modules; a bundled service uses `models/streaming-zipformer`.

The source tree contains no weights. `service/MODEL_MANIFEST.json` pins the official release archive, SHA-256, constituent hashes and the model card declaring Apache-2.0. `python3 scripts/download_model.py` verifies and extracts the archive and regenerates its public BPE vocabulary; `--archive` accepts a previously downloaded copy. The four model assets and derived vocabulary match the maintained baseline.

`service/com.simonli.voicefly.service.plist` is a launch template, not ready to load: `__HOME__` must be replaced with the target user's actual home directory. The combined installer fills in user-specific startup configuration; normal runtime data is local.

## Verification policy

Synthetic unit tests, build verification, real keyboard/host checks, microphone/device checks, and clean installation answer different questions. Record the actual environment and outcome for each relevant stage. Never attach the maintainer's application, model, recovery archive, private ledger, or local environment as a substitute for a public build.

## Build the application bundles and installer

```sh
python3 scripts/build_settings.py
VOICEFLY_MODEL_DIR=/absolute/path/to/verified/public-model python3 -m PyInstaller --noconfirm --workpath build/service --distpath dist service/VoiceFlyService.spec
python3 scripts/build_installer.py
```

These steps require the already prepared frontend and the runtime dependencies. They do not install the applications. The service specification bundles only named public model and starter files; personal words are migrated on the user's computer after installation. Preserve application symlinks and signing metadata when copying bundles (`ditto --rsrc --extattr` on macOS). Do not insert a maintainer's installed service into a release payload.
