# Contributing to Roxi

Start with the [quickstart](README.md#quickstart) and [technical specification](docs/PROJECT-SPEC.md). Keep changes focused and describe how a reviewer can reproduce them. Use fictional or explicitly publishable fixtures. Keep local archives, recordings, model weights, and credentials out of Git.

## Test a change

From the checkout, with Python 3.13 and the dependencies installed:

```powershell
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

The pull-request and main-branch workflows run unit tests on Linux and Windows. The Linux integration check starts disposable PostgreSQL, imports a generated test fixture, accepts and publishes review decisions, and scores the assistant's answers and source citations. It compares HTTP and MCP outputs and checks live chat recovery, source freshness, quarantine, and parser replay. CI uses deterministic test PCM and does not measure speech recognition. Use the [pilot evaluation](docs/PILOT-EVALUATION.md#repeat-the-checks) to test local speech-to-text.

You can run the CI smoke check locally after creating a fresh disposable PostgreSQL database in the development container. In a new PowerShell session, replace `roxi_smoke` with the name of that test database and use the local fixture credentials.

```powershell
docker compose exec -T db createdb -U review_owner roxi_smoke
$env:EXERCISE_REVIEW_DATABASE_URL = 'postgresql://review_owner:review-local-owner@127.0.0.1:55432/roxi_smoke'
$env:EXERCISE_REVIEW_EDITOR_URL = 'postgresql://review_editor:review-local-editor@127.0.0.1:55432/roxi_smoke'
$env:EXERCISE_REVIEW_READER_URL = 'postgresql://review_reader:review-local-reader@127.0.0.1:55432/roxi_smoke'
.venv/Scripts/python.exe tools/smoke_demo.py
.venv/Scripts/python.exe tools/check_live_replay.py
.venv/Scripts/python.exe tools/check_source_catalog.py
```

## Cut a release

1. Give the version its own `## vX.Y.Z` changelog heading and merge the changes to `main`.
2. Wait for the tests on `main` to pass. Tag the tested commit and push the single tag.

   ```powershell
   git tag -a v0.1.0 -m 'Roxi v0.1.0'
   git push origin refs/tags/v0.1.0
   ```

3. Check the **Release** run. It reruns the test workflow on the tagged source and uses that version's changelog text to publish the GitHub release. GitHub provides the source archives. The release includes the fixture generator and scripts.
