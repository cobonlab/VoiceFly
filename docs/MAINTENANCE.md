# Voice Fly maintenance evidence

This summary reconstructs local development before the first public source release. These dates are not public release dates, and the repository does not manufacture or backdate commits. Public maintenance begins with the actual initial source commit.

| Period in 2026 | Maintained work |
| --- | --- |
| August | Moved to a native macOS input method, integrating Squirrel/Rime, Flypy typing, and local streaming ASR |
| Late August to early September | Added explicit phrase-length learning, candidate interaction, decomposition mode, and shared personal-term handling |
| September 3–8 | Separated generated and runtime vocabulary ownership; added durable personal records, local mirrors, and bounded hotword refresh |
| September 9 onward | Added optional second-model research and validation scaffolding; baseline finalization remains the default |
| September 22–23 | Addressed microphone selection, separated capture from decoding, protected numeric expressions, and improved streamed text stability |
| September 28–29 | Investigated premature Control release behavior and installation-time stranded recording state; one physical-key approach was reverted after compatibility problems |
| September 30 | Prepared a public source snapshot, retained dependency revisions and patches, excluded private data, and documented the independent-use roadmap |

## Validation evidence and its limits

Before publication, the exported Swift core passed 52 tests, and an eight-module Python selection passed 208 tests in the existing local development environment. The selection covers audio, hotwords, personal phonetics, asynchronous service refresh, streaming protocol, optional refinement, numeric formatting, and text rules. Original source SHA-256 values are preserved in `docs/provenance/source-preview-20260930.json`.

The portable Python subset is the initial public CI gate. It requires only NumPy and does not load microphone hardware or ASR models. The full Python selection also imports macOS and ASR/text-normalization dependencies; that environment is not yet a clean-machine installation guarantee.

On September 30, the public snapshot passed the Swift core's 52 tests and ran 141 portable Python tests in a fresh Python 3.13 virtual environment with NumPy 2.5.2: 140 passed and one platform-related test was skipped. The official pinned dependency preparation and librime patch application also completed successfully. That paragraph records the first source preview. For 0.2.0, the native frontend/settings/service builds, isolated migration, fresh-profile deployment and six actual Rime candidate checks have passed. The current portable suite runs 144 tests (143 passed, one skipped), and the Swift core ran 60 tests without failure. Combined installation validation is recorded separately as it completes.

Neither the test counts nor local runtime use establish public adoption. They also do not prove new-user installation, code signing, cross-device performance, third-party data redistribution, or physical-key behavior in every application. Outstanding space and arrow-key reports remain on the roadmap until reproduced and resolved.

## Maintainer installation check for 0.2.0

The combined installer installed the three current Voice Fly applications on the maintainer's Apple Silicon Mac running macOS 26.6.2. The migration retained all 6,365 personal records; these records are private and are not release inputs. After logging out and back in and approving macOS's input-method prompt, the maintainer confirmed working typing and normal Control-triggered dictation on October 1, 2026 (Asia/Shanghai). A separate, earlier Squirrel installation initially remained selected; it was disabled and archived while Voice Fly stayed selected. This is one maintainer-machine check, not a clean-machine or new-user result.

## October 1 source-preview update

The final source snapshot was checked again in Python 3.13.7 with NumPy 2.5.2: 144 tests ran, 143 passed and one was skipped. A separate Swift scratch build ran 60 tests with no failures. Public scheme checksums matched their pinned source inventory, and the candidate contained no maintainer home path, private runtime ledgers, recordings, model weights, application installers or backups.

Frontend build 26 uses the approved framed microphone-and-wind menu badge. The earlier opaque black-on-white badge was reported as a blank white rectangle in the native menu; vector alpha cutouts fixed that display. The maintainer confirmed the final native icon, Chinese typing and Control dictation all normal without logout. Only the input frontend/settings and menu display were refreshed; the speech service was kept running. Other OS versions, fresh installation and full-component hot upgrades remain unverified.

## Ongoing maintenance responsibilities

Simon Li maintains input/session integration, audio lifecycle, explicit learning safeguards, numeric handling, dependency coordination, tests, documentation, and release preparation. Codex has assisted development and review since August; the maintainer remains responsible for merging and releasing changes.

Each future update should link to its actual commit or issue, describe the user-visible effect, record validation and remaining limitations, and update the roadmap. There is no invented community size, download count, or prior public release history.

## October 1 complete installer preview

Prepared 0.2.0-preview.3 after the source preview. Found and removed a real new-user dependency: the frontend's glog/gflags links previously pointed at Homebrew. The staged package now includes those libraries and their notices, pinned common Rime resources and the complete public model/runtime. Local acquisition metadata and unused optional plugins are excluded.

The extracted package's six keyboard schemes produced expected greeting candidates under a restricted process that could not read Homebrew or the old development directory. Its speech executable verified five model hashes, ran generated-silence inference, passed 24 normalization and seven synthetic phonetic cases, with network and external development/Python paths blocked. The Python regression subset ran 144 tests on this Mac, all passed. No real recording or independent first-install outcome is claimed by these checks. The installer keeps logout manual and was not used to log out the maintainer's current session.

Publication is driven by a completed usability milestone. There is no documented OpenAI program requirement for a particular release interval; progress is recorded truthfully instead of manufacturing release activity.


## October 13, 2026: keyboard reliability maintenance

Daily-use reports in Codex, Telegram, WeChat and web text fields prompted frontend build 27. Held Space no longer counts as double/triple taps; deferred spaces use a composition-aware commit. With no composition, Left/Right goes directly to the host without unnecessary Rime/UI refresh. Commit and activation clear cached caret/selection. Installer user commands now enter the logged-in GUI bootstrap environment: a read-only comparison showed that plain sudo reported stale Voice Fly selection while the GUI environment correctly reported ABC.

The maintainer-only frontend update succeeded with a build-26 backup, valid signatures, matching installed binary and unchanged speech-service process. Public preview.4 is independently repackaged from the rebuilt frontend and reviewed public resources; the installed bundle and private backup are not release inputs. Local Swift: 63 passed. Local Python 3.14: 144 run, 143 passed, one skipped; GitHub CI separately runs Python 3.13. Extracted installer integrity/dependency/architecture checks passed. Packaged speech checks passed five hashes, generated-silence inference, 24 normalization and seven synthetic phonetic cases without microphone/service startup. Exact-commit cloud status is recorded by GitHub.

Real-use confirmation across all reported text fields remains pending. The existing single-Space delay of up to 0.55 seconds is retained with multi-tap learning. This maintenance release does not establish universal host compatibility or independent first installation.
