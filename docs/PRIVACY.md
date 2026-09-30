# Privacy and current limitations

The baseline speech recognizer runs locally and communicates with the input method using macOS distributed notifications. It does not call an OpenAI transcription endpoint. Audio buffers used for streaming and optional finalization are temporary working memory; explicit research capture tools can save audio when deliberately invoked.

Personal learning is explicit: ordinary typing-selection statistics do not automatically become speech corrections. The repository excludes personal dictionaries, runtime ledgers, recordings, transcripts, logs, model weights, credentials, and private Git history.

Current limitations are part of the early release:

- Personal words and settings now default to local user storage. The one-time upgrade reads the old installation/iCloud records to preserve words; normal operation does not depend on iCloud. Optional synchronization is not implemented.
- Routine learning failures use fixed diagnostics rather than printing the failed phrase. The health file contains a phase, time, process ID and fixed error category, with no dictation text. Rime diagnostics and explicit research tools may still contain paths or private data; review before sharing.
- The manual smoke utility prints recognized text. Research capture/evaluation utilities are explicit tools and can process recorded audio; they are not the ordinary input-method defaults.
- Optional refinement adapters can connect to a user-configured endpoint. The mode is disabled by default. Enabling an adapter changes what may be transmitted and requires a deliberate review of the chosen endpoint.
- macOS distributed notifications carry live composition text between local processes. This mechanism should not be described as an encrypted private channel between only two authenticated processes; reviewing sender/session validation is planned maintenance.
- When macOS reports secure input, voice capture and personal learning are suppressed. Physical-key and real-host validation across applications remains required.
- The recognizer cannot identify the maintainer's voice or reliably exclude other people and media.

Use synthetic examples in issues and contributions. Before attaching diagnostics, review and remove recognized text, personal terms, account identifiers, device names, paths, audio, credentials, and unrelated system information.
