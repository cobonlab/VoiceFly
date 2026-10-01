# Third-party notices and source provenance

Voice Fly's original code and documentation in this repository are released under GPL-3.0-only. The Squirrel-derived frontend retains its existing GPLv3 license and attribution. This declaration does not replace a third party's copyright or license, and does not cover external model weights, vocabulary packs, or separately licensed artwork.

| Component | Source and use | License treatment |
| --- | --- | --- |
| Squirrel | Modified frontend based on the revision recorded as `input_method_base` in `SOURCE_MANIFEST.json` | Original GPL text remains at `input-method/LICENSE.txt`; original upstream README credits remain in that directory |
| librime | Pinned Rime engine; local selector patch in `patches/librime.patch` | BSD 3-Clause text retained in `licenses/librime.LICENSE`; dependencies fetched separately |
| Sparkle | Pinned update framework; current updater is disabled | Its MIT and bundled external notices retained in `licenses/Sparkle.LICENSE` |
| plum | Pinned Rime data preparation tooling | LGPLv3 text retained in `licenses/plum.LICENSE`; downloaded dictionary packs retain their own terms |
| X-ASR | Public Chinese-English streaming int8 model, pinned in `service/MODEL_MANIFEST.json` | Model artifact page declares Apache 2.0; the installer bundles the verified files and Apache license under `ThirdPartyLicenses/x-asr/`. Source archives contain the manifest, not weights. |
| sherpa-onnx / ONNX Runtime | Bundled speech runtime | Apache 2.0 / MIT; runtime licenses and ONNX Runtime external notices are included in the service bundle |
| NumPy / sounddevice / PyObjC / CFFI / Python | Bundled service dependencies | Retained package license texts, including NumPy external notices, Python license and libffi/PortAudio notices |
| WeTextProcessing / Pynini / OpenFST | Bundled Chinese normalization dependencies | Apache 2.0 notices and exact source/version reference are retained; fresh development-machine rebuilding remains unverified |
| PyInstaller | Service packaging | GPLv2-or-later with distribution exception; COPYING text included |
| Rime full-pinyin and double-pinyin resources | Official `rime-pinyin-simp`, `rime-double-pinyin`, `rime-luna-pinyin`, and `rime-terra-pinyin` sources at pinned revisions | Apache 2.0, GPLv3, or LGPLv3 as supplied by each repository; original `LICENSE`, `AUTHORS` and README retained under `third-party/rime-schemes/` |
| Rime Wubi resources | Official `rime-wubi` source at a pinned revision | LGPLv3 license, authors and README retained under `third-party/rime-schemes/rime-wubi/` |

Repository links: [Squirrel](https://github.com/rime/squirrel), [librime](https://github.com/rime/librime), [Sparkle](https://github.com/sparkle-project/Sparkle), [plum](https://github.com/rime/plum), [X-ASR](https://github.com/Gilgamesh-J/X-ASR), [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

The source export intentionally omits private production dictionaries and their generated artifacts. The small spoken-number idiom exception list is a reviewed generic program resource; it contains no personal learning records. Public scheme files, wrappers and generated pronunciation/code mappings are included in `input-method/data/voicefly/`; the pinned source checksums are in `third-party/rime-schemes/SOURCES.json`. Voice Fly's wrappers are modified from those sources and identify the modification in their header. `word-codes.json` is generated from the same public pinyin and Wubi resources, not from the maintainer's personal dictionary.

Model weights are excluded. `service/MODEL_MANIFEST.json` records the model release URL, checksums and upstream license declaration for local acquisition; this source release does not redistribute a model or generated application. Additional data packs still require their own source and license review before inclusion.

The active Voice Fly microphone-and-wind artwork is original and maintainer-approved. Underlying Rime/Squirrel names and code retain their attribution. No affiliation or endorsement by upstream maintainers or OpenAI is implied.

## Complete installer preview

The 0.2.0-preview.3 installer includes reviewed public weights and runtime binaries. Each application retains relevant license notices; no personal dictionary, recording or runtime ledger is an input. `third-party/binary-notices/` holds additional notices and native library source records. glog 0.7.1 and gflags 2.3.1 are embedded with BSD notices; their staged load paths are changed for portability. The optional Lua/octagram/predict plugin binaries from older development checkouts are not shipped.

Pinned common Rime prelude, essay and stroke source files and their original licenses are retained in `third-party/rime-common/`, with commits and SHA-256 values in its `SOURCES.json`. OpenCC dictionaries are generated from the maintained OpenCC source dependency and retain its Apache 2.0 license. Source-release exclusions described above still apply to source archives; the separately attached installer contains its verified public model and dependencies.
