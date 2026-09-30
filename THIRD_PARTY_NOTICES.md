# Third-party notices and source provenance

Voice Fly's original code and documentation in this repository are released under GPL-3.0-only. The Squirrel-derived frontend retains its existing GPLv3 license and attribution. This declaration does not replace a third party's copyright or license, and does not cover external model weights, vocabulary packs, or separately licensed artwork.

| Component | Source and use | License treatment |
| --- | --- | --- |
| Squirrel | Modified frontend based on the revision recorded as `input_method_base` in `SOURCE_MANIFEST.json` | Original GPL text remains at `input-method/LICENSE.txt`; original upstream README credits remain in that directory |
| librime | Pinned Rime engine; local selector patch in `patches/librime.patch` | BSD 3-Clause text retained in `licenses/librime.LICENSE`; dependencies fetched separately |
| Sparkle | Pinned update framework; current updater is disabled | Its MIT and bundled external notices retained in `licenses/Sparkle.LICENSE` |
| plum | Pinned Rime data preparation tooling | LGPLv3 text retained in `licenses/plum.LICENSE`; downloaded dictionary packs retain their own terms |
| X-ASR | External baseline speech model source | Upstream repository declares Apache 2.0; no code or weights from its repository are bundled here; individual deployment-weight terms require verification before redistribution |
| sherpa-onnx | Runtime imported by the service | Apache 2.0 package; no runtime binary bundled in this source release |
| NumPy / sounddevice / PyObjC | Runtime dependencies | Package licenses retained by their distributions; installed separately |
| WeTextProcessing / Pynini | Chinese normalization dependencies | Installed separately; exact development version and dependency reproducibility remain documented build work |
| PyInstaller | Future service packaging | Installed separately, subject to its license and distribution exception; no generated service binary included |
| Rime full-pinyin and double-pinyin resources | Official `rime-pinyin-simp`, `rime-double-pinyin`, `rime-luna-pinyin`, and `rime-terra-pinyin` sources at pinned revisions | Apache 2.0, GPLv3, or LGPLv3 as supplied by each repository; original `LICENSE`, `AUTHORS` and README retained under `third-party/rime-schemes/` |
| Rime Wubi resources | Official `rime-wubi` source at a pinned revision | LGPLv3 license, authors and README retained under `third-party/rime-schemes/rime-wubi/` |

Repository links: [Squirrel](https://github.com/rime/squirrel), [librime](https://github.com/rime/librime), [Sparkle](https://github.com/sparkle-project/Sparkle), [plum](https://github.com/rime/plum), [X-ASR](https://github.com/Gilgamesh-J/X-ASR), [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

The source export intentionally omits private production dictionaries and their generated artifacts. The small spoken-number idiom exception list is a reviewed generic program resource; it contains no personal learning records. Public scheme files, wrappers and generated pronunciation/code mappings are included in `input-method/data/voicefly/`; the pinned source checksums are in `third-party/rime-schemes/SOURCES.json`. Voice Fly's wrappers are modified from those sources and identify the modification in their header. `word-codes.json` is generated from the same public pinyin and Wubi resources, not from the maintainer's personal dictionary.

Model weights are excluded. `service/MODEL_MANIFEST.json` records the model release URL, checksums and upstream license declaration for local acquisition; this source release does not redistribute a model or generated application. Additional data packs still require their own source and license review before inclusion.

The active Voice Fly microphone-and-wind artwork is original and maintainer-approved. Underlying Rime/Squirrel names and code retain their attribution. No affiliation or endorsement by upstream maintainers or OpenAI is implied.
