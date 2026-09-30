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

Before publication, the exported Swift core passed 52 tests, and an eight-module Python selection passed 208 tests in the existing local development environment. The selection covers audio, hotwords, personal phonetics, asynchronous service refresh, streaming protocol, optional refinement, numeric formatting, and text rules. Original source SHA-256 values are recorded in `SOURCE_MANIFEST.json`.

The portable Python subset is the initial public CI gate. It requires only NumPy and does not load microphone hardware or ASR models. The full Python selection also imports macOS and ASR/text-normalization dependencies; that environment is not yet a clean-machine installation guarantee.

On September 30, the public snapshot passed the Swift core's 52 tests and ran 141 portable Python tests in a fresh Python 3.13 virtual environment with NumPy 2.5.2: 140 passed and one platform-related test was skipped. The official pinned dependency preparation and librime patch application also completed successfully. The full frontend build and combined installer remain unverified.

Neither the test counts nor local runtime use establish public adoption. They also do not prove new-user installation, code signing, cross-device performance, third-party data redistribution, or physical-key behavior in every application. Outstanding space and arrow-key reports remain on the roadmap until reproduced and resolved.

## Ongoing maintenance responsibilities

Simon Li maintains input/session integration, audio lifecycle, explicit learning safeguards, numeric handling, dependency coordination, tests, documentation, and release preparation. Codex has assisted development and review since August; the maintainer remains responsible for merging and releasing changes.

Each future update should link to its actual commit or issue, describe the user-visible effect, record validation and remaining limitations, and update the roadmap. There is no invented community size, download count, or prior public release history.
