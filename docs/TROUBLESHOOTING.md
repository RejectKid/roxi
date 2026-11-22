# Troubleshoot the local demo

| Symptom | What to check |
| --- | --- |
| Missing `facilities-radio.wav` or `unaligned-radio.wav` | Run `powershell.exe -NoProfile -File tools/Build-DrillAudio.ps1` from the checkout. The WAVs are generated locally. |
| The generator cannot select a voice | In Windows PowerShell 5.1, run `Add-Type -AssemblyName System.Speech` followed by `(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices().VoiceInfo.Name`. Check for **Microsoft David Desktop** and **Microsoft Zira Desktop**. |
| PostgreSQL connection refused | Start Docker Desktop and run `docker compose up -d --wait db`. Check `docker compose ps` and `docker compose logs db`. The default host port is **55432**. |
| The app returns 503 or `/healthz` fails | Ensure PostgreSQL is healthy and run `.venv/Scripts/python.exe -m review_app migrate`. Start the app again with the same database settings used to migrate. |
| Import reports one diagnostic | The demo intentionally includes a malformed chat timestamp on line 24. Open **Source coverage** to see it. Reimporting the same files should report zero new source versions. |
| The review queue shows a draft conflict | Reload the queue, check the latest version, and reapply the edit. Accept an earlier event before saving a link to it. |
| The assistant or developer API has no published revision | Extract proposals, accept the relevant events, and publish from **Review queue**. Check the exercise ID and the revision selected in the assistant. |
| A 404 occurs only in MCP | Verify the MCP executable, its `PYTHONPATH`, reader database URL, and exercise allowlist. Its archive directory must match the HTTP app's archive. |
| An original-source link returns 410 | Check that the configured archive still contains the imported bytes. Restoring the original archived file restores the link. Reimporting changed bytes creates a different source version. |
| A free-form question falls back to rules | Check that Ollama is serving at `127.0.0.1:11434` and the configured local model is installed. Run `ollama list`. The demo's explicit questions also work without Ollama. |

Stop the foreground app with **Ctrl+C**. You can stop PostgreSQL with `docker compose stop db` and restart it with `docker compose up -d --wait db`. That keeps the database volume. Start the HTTP app again using the final command from the [quickstart](../README.md#quickstart).
