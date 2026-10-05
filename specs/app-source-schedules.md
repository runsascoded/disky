# Independent scan sources for disky

Status: design ready; implementation follows the first M1 pilot. Requested 2026-10-05. App work belongs on `tauri-native-app`; shared reader and data contracts flow through `local` to its sibling `m3`. NAS connection details and deployment credentials belong on m3, outside tracked source.

## Product behavior

One installation manages several sources: this Mac, an external volume, a mounted network share, or a directory reached over SSH. Each has a stable ID, display name, enable switch, daily or weekly schedule, manual Scan now action, retention policy, destination, and independent progress and last-result state. The viewer selects a source before selecting its snapshot; paths, history and diffs never mix different sources.

Keep one per-user LaunchAgent as a scheduler. It periodically finds due sources and runs them serially initially, with a process lock and a per-source lease. Users configure times and weekdays, rather than having to understand cron syntax or install an agent for every volume. Missed slots coalesce into one run after wake; adding or enabling a source does not scan immediately unless requested. A source without an earlier run becomes due at its next configured slot. Manual scans do not enable scheduling.

An unavailable volume or host is an offline skip, preserves the latest successful snapshot and gets one bounded retry at a later scheduler wake. No busy loop, no repeated cloud ingest of an unchanged snapshot, no empty replacement snapshot, and no blocking other sources. A failed scan remains visible separately from offline status. Retention only removes successful snapshots of the same source after a new snapshot has been published atomically.

## Source identities and data

Add versioned `sources` to `disky.json`, with IDs generated once and persisted. A source carries its backend/root, schedule, enabled intent, retention and optional capture destination. Keep run state and progress per source. Write local snapshots under `scans/sources/<id>/<snapshot-id>`, and carry source ID, backend, root and host into metadata and capture manifests. Snapshot IDs must distinguish runs within a minute; keep snapshots immutable and test same-minute scans.

Migrate the existing implicit scan into a single `this-mac` source without changing explicit `settings.site`, `settings.scope`, schedule, `jobs.scan` upload credentials, follow-on commands or drainer settings. Preserve existing snapshots and opt-in scheduling state. Existing source-free data is treated as that source; do not duplicate a large index just to migrate its layout. An app upgrade must not start a scan or silently enable an old disabled agent.

Extend the Rust HTTP server's page configuration and store registry to enumerate sources and route API requests to each source's scan directory. Home shortcuts and titles come from the chosen source. A remote Linux home is not this Mac's home. The API validates source IDs and snapshot IDs; clients do not send arbitrary filesystem roots for reads.

## Mounted volumes

Use volume identity, not only a mount path. Remember its UUID where available and validate that the requested root is actually on the expected mounted filesystem before walking. In particular, `/Volumes/NAS` being absent or replaced by a directory on the boot disk is not a successful NAS scan. Network shares without a usable UUID require a verified mount source plus a root relative to that mount. Disconnects during a scan fail that run and do not publish partial output. Continue to show cached snapshots while unplugged.

The native walker is optimized for macOS filesystems. Benchmark it on SMB/NFS before choosing mounted scanning as the recommended NAS transport. Do not infer APFS extent or clone properties for a network filesystem.

## SSH

Prefer running the metadata walk on the NAS and streaming records to this Mac when the NAS supports an appropriate lister. This avoids a network metadata round trip per file. Reuse `/usr/bin/ssh`, the user's SSH aliases and agent/Keychain; store an alias and root, not passwords or private-key contents. Scheduled runs use batch mode, strict known-host verification and connection/time limits. The user completes first connection/host trust interactively; background scans never prompt.

Do not assume an ARM macOS walker runs on a NAS, or that the NAS has GNU find. Probe the OS and available lister first. Use a tested GNU find null-delimited allocated-block record format where available, or a separately built compatible remote helper. Quote the remote root as a literal shell argument, reject option-looking host aliases, and never derive shell commands from unescaped paths. Nonzero SSH/lister exits and stderr are reported; interrupted streams cannot publish success.

The metadata stream feeds the same Rust reducer and capture schema as the local walk. No file contents are downloaded. Byte semantics are explicit per backend: allocated bytes when the remote filesystem/lister reports them, otherwise a clearly labeled logical size mode. NAS snapshots stay in an independent source even if their paths resemble paths on this Mac.

## Delivery and verification

1. Source model, migration and CLI scheduling with exact tests for due slots, wake catch-up, disabled sources, offline sources and overlapping runs.
2. Per-source local storage and HTTP/store selection; fixtures prove that identical paths in two sources have independent history and diffs.
3. Settings source editor, daily/weekly schedules, enable controls and per-source Scan now; verify the native Settings window and browser viewer.
4. Mounted-volume identity checks and disconnect tests.
5. SSH lister protocol with fixtures for spaces, quotes, newlines, non-ASCII names, stderr and interrupted streams; then a real NAS trial from m3.

For the m3 trial, start with this Mac twice daily and NAS weekly or manually; tune from the measured scan duration and host load. Remote NAS walking over SSH is the preferred candidate, subject to the actual NAS OS/lister. A separate deployment is useful for credentials, cloud destinations or a machine that remains on; it is not the source scheduling abstraction.

Read-only discovery on 2026-10-05 verified that the existing `nas` Tailscale SSH alias reaches the Unraid NAS (Linux x86_64) with batch mode and strict known-host checking. GNU find 4.10.0 is installed. `/mnt/user` has `video`, `fcp`, `photos`, `backups`, `system` and `appdata`; no share is mounted on this Mac. The `tower` LAN alias did not resolve from the current network. Do not scan both `/mnt/user` and the individual disk/pool paths: they expose overlapping content.

Required before enabling the real NAS schedule: which shares to include, desired frequency and whether its metadata should stay local or upload. Recommend weekly at first and local metadata, with a manual connection/scan check before enabling the schedule. No NAS scan or recurring job has been started by this discovery.
