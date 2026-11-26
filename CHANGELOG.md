# Changelog

## v0.2.0

- Receive independently scoped chat messages and completed radio WAVs. Save originals before classification and transcription, and recover queued audio after a restart.
- Query mixed communications, asset and site registers, and civilian fleet observations by event time and known time. Read typed reports, proposed links, conflicting observations, and coverage gaps.
- Embed live and reviewed answers through versioned HTTP. Use Python or read-only MCP to query and reopen cited evidence. Reconnect to history changes with a saved cursor.
- Provision authenticated workspaces with source grants and revocation, connector health, retention, and backup and restore tooling.
- Replay the fictional multi-channel exercise and run the same HTTP, Python, MCP, and original-evidence contract checks in CI.
- Run a standalone integration lab that sends microphone, chat, and simulated fleet data and queries Roxi through a companion assistant.

## v0.1.0

- Review the fictional West Annex drill on a synchronized radio and chat timeline. Play clips at their original offsets.
- Extract and review reports, corrections, and acknowledgments. Publish revisions with links to the original evidence.
- Query the same published revision over HTTP and read-only MCP.
- Generate local WebVTT with Faster Whisper and ask questions through the assistant. The independent verifier checks evidence through MCP.
- Run the demo on Windows from the documented quickstart. Synthesize the fixture WAVs locally from the supplied scripts.
