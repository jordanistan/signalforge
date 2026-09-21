# SignalForge implementation status

Last updated: 2026-09-21. This is the implementation handoff for the current build-out.

## Repository assessment

The workspace already implements mesh messaging, a Textual terminal UI, a browser command center,
offline maps, SDR surveying/audio, ADS-B, pager/environment sensor decoding, NWS weather,
satellite tracking, and follow-topic bookkeeping. There is no separate product specification.
The current milestone completes reliability, setup, capability reporting, and verification of those workflows.

The `.git` directory in the supplied workspace is empty and read-only; Git status, commits, and
diff checks are unavailable here. Radio configurations and firmware were not changed.

## Implemented in this milestone

- Pinned direct dependency manifests, `make verify`, and read-only dependency diagnostics.
- Server and bridge reliability: byte-safe payloads, recoverable outbox processing, persisted delivery state,
  malformed-history recovery, startup validation, and isolated demo operation.
- Independent SDR capability checks, process ownership/cleanup, bounded decoder state, and parser coverage.
- Weather partial-failure reporting and satellite cache/location/pass correctness, with offline fixtures.
- Browser capability states, page lifecycle, responsive layouts, and coverage of the newer pages.
- Updated setup, operations, data handling, and hardware acceptance documentation in README and OPERATIONS.md.

## Verification

Completed verification: **403 unique tests passed** across the backend, browser, CLI, and final regression runs.

- Backend/terminal/service tests: **349 passed** in 54.03 s.
- Final server/runtime/CLI regression run: **63 passed** in 7.60 s, including the additional test proving
  failed radio handshakes release the serial port before reconnecting. Most tests overlap the counts above/below.
- Browser tests: **48 passed** across a full 47-test run (70.62 s) and the final additional capability test.
- Python, shell, and all seven JavaScript files pass syntax checks.
- Installed dependency consistency check passed; all pinned dependencies are present in the current environment.
- `make doctor` ran successfully; installed feature dependencies are available. The serial path was unavailable
  to that diagnostic, so hardware connectivity is not claimed.
- Production CLI: **5 passed** in 1.99 s, covering invalid settings, real server startup,
  authenticated HTTP/WebSocket access, isolated demo sending, token file permissions, and clean shutdown.

Automated tests use fake serial/SDR receivers and canned external feeds. No physical transmission,
firmware flashing, or service deployment is part of this implementation run.

Known verification warning: Skyfield 1.55 uses an array dtype assignment deprecated by NumPy 2.5.
It does not fail the tests; the warning is not suppressed. Fresh network installation of the manifests
was not performed; the installed environment satisfies them.

The original browser fixture wrote its generated survey summary to `run/radio-activity.json` during the
initial run. The fixture now writes to a temporary directory. The prior transient survey summary was not
captured and could not be restored; mesh messages, node snapshots, configuration backups, and firmware
were not intentionally modified by verification.

## Operational boundaries

- Only one process can own a serial radio; only one mode can own an SDR dongle.
- Follow mode stores requests and records checks; an external assistant/session must actually research
  topics and queue updates. Starting the web server does not start a research worker.
- Cached maps and orbital calculations can operate offline; weather, fresh TLEs, and street/radar tiles need internet.
- Hardware-dependent behavior needs a separate acceptance pass with the intended devices and antenna.
- Runtime logs, exported radio keys/configuration, and bearer URLs are local private data, excluded from version control.
