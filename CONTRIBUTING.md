# Contributing to Voice Fly

This is an early public source release. Useful contributions include reproducing input/audio problems, making standalone tests independent of local configuration, improving developer setup, implementing visual settings, adding typing schemes with correct learning, and preparing installation/recovery workflows.

Before a large change, open an issue with the intended user-visible behavior. Keep changes small enough to review, include relevant validation, and state what still needs physical-key or microphone testing. Update the roadmap and documentation when behavior changes.

Run the Swift core tests and public Python subset documented in `docs/BUILD.md` when changing the relevant code. Full ASR integration tests need the reference development environment. Never claim that a unit test proves a real application or microphone behavior that it did not exercise.

Do not include personal vocabulary, production profiles, audio, transcripts, identifying logs, private credentials or model weights in a pull request. Use synthetic fixtures. Generated dictionaries and runtime personal records have different owners; changes must preserve that separation and existing user data.

Existing file notices and upstream licenses must be retained. Contributions must be code or material you have the right to contribute and must be compatible with the applicable licenses. No contributor agreement or copyright transfer is required by this document.
