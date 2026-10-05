# disky — native macOS app

Disky is a Tauri v2 menu-bar app with an in-process Rust filesystem scanner and indexer. It serves the bundled React site over loopback and supports local treemaps, exact path filters, diffs and size history. No Python, Homebrew `gfind`, checkout or cloud account is needed to run local mode.

The app branch (`tauri-native-app`, `wt/app`) merges `local`; `m3` is a sibling deployment branch that also merges `local`. Shared site/engine changes travel upstream to `local`; machine-specific upload destinations and deployment configuration stay on m3.

## Download and pilot

Download an Apple Silicon pilot from [GitHub Releases]. See [the pilot instructions][pilot] for installation and the M1 checklist. Pilot artifacts are ad-hoc signed, not notarized; Developer ID signing and notarization are still needed for smooth public installation.

## Build

Requirements: macOS, Rust, Xcode Command Line Tools, Node and pnpm. The pinned Tauri CLI is `2.11.4`.

```bash
# From the repository root.
pnpm install --frozen-lockfile
cargo install tauri-cli --version 2.11.4 --locked
cargo test --manifest-path apps/tauri/Cargo.toml --workspace --release --locked -j 2
apps/tauri/scripts/package
node apps/tauri/scripts/smoke.mjs
```

`package` builds the Rust walker, the local SPA and the app; produces a DMG, `.app.zip` and `SHA256SUMS` under `tmp/releases/disky-<version>-<arch>`; and verifies the app signature. It defaults to ad-hoc signing (`-`). Set `APPLE_SIGNING_IDENTITY` to use a certificate already installed in your Keychain. Tauri's Apple notarization environment variables apply when configured. Packaging does not install the app or alter registered agents.

`smoke.mjs` exercises the bundle in a new scratch home with only system tools on PATH: defaults, a native fixture scan, local API and bundled SPA. Pass an extracted `.app` path to test a ZIP's contents. It creates fixtures under `tmp/app-smoke` and never changes your real settings or scans.

The [macOS workflow][workflow] tests and packages on an Apple Silicon macOS 15 runner. Pushes to the app branch upload CI artifacts; tags `disky-v<version>` publish a pilot prerelease only after the build and extracted-bundle smoke check pass. The tag must identify the app branch, not the default `cloud` branch.

## Runtime

- `crates/dt-walker`: native `getattrlistbulk` filesystem walk, inside the app's signed identity.
- `crates/dt-capture`: optional Parquet capture to a local directory or S3/R2 destination.
- `crates/dt-index`: Rust index/reduce, row-group reads, filters, diffs and local HTTP API.
- `src-tauri`: windows, tray, Full Disk Access onboarding, jobs and agents.
- `settings`: the bundled Settings page; `web` is the generated local site bundle.

Fresh installs default to local mode. Existing explicit prod/dev settings are preserved. Local scans live in `~/Library/Application Support/disky/scans`; settings/jobs in `~/.config/disk-tree/disky.json`; logs in `~/Library/Logs/disk-tree`. Scheduled scans use plain per-user LaunchAgents and one global daily schedule today. The SMAppService login item is separate. The [independent source design][sources] covers volumes, SSH and NAS scheduling.

```bash
# The installed app's executable; these modes don't initialize a GUI.
disky version
disky settings show
disky job scan
disky scan now
disky serve --addr 127.0.0.1:7793
disky probe
disky agents register|unregister|status
disky login-item on|off|status
disky agent -- COMMAND ARGS…
```

`disky serve` serves the bundled SPA and local scans on loopback for browser diagnostics. `DISK_TREE_ROOT` selects another configuration directory; `DISKY_URL` overrides the viewer. A configured scan job can index locally (`local: true`), upload (`to`), or do both in one walk. Selecting a viewer does not create an upload destination.

For the stable self-signed development identity and the existing m3 installation, `cargo tauri build --bundles app` retains `disk-tree-selfsigned` from `tauri.conf.json`. `scripts/agentctl install` installs into `~/Applications`, re-registers agents and refuses to interrupt a running scan unless forced. A pilot build is kept separate until tested.

Design/status: [Rust engine][rust-engine], [native app][native-app], [app-link contract][app-link].

[GitHub Releases]: https://github.com/runsascoded/disky/releases
[pilot]: PILOT.md
[workflow]: ../../.github/workflows/macos-app.yml
[rust-engine]: ../../specs/rust-engine.md
[native-app]: ../../specs/tauri-native-app.md
[app-link]: ../../specs/app-link.md
[sources]: ../../specs/app-source-schedules.md
