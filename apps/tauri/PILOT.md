# disky — Apple Silicon pilot

disky is a native macOS menu-bar app that scans your disk and shows treemaps, search, sizes over time and differences between scans. This pilot runs entirely on your Mac: no Python, Homebrew, source checkout, account or cloud credentials are needed.

This is an **Apple Silicon prerelease**, configured for macOS 13 or later. The first external test machine is an M1 MacBook. Intel and App Store builds are not included.

## Install

1. Download the `arm64.dmg` asset, open it, and drag **disky** into Applications. Alternatively, extract the `arm64.app.zip` and move **disky.app** into Applications.
2. Open disky. This pilot is **ad-hoc signed and not notarized**, so macOS may require **Open Anyway** in System Settings → Privacy & Security. Only approve the app you downloaded from this repository's release. A Developer ID signed and notarized release will follow.
3. In disky's Settings, choose the whole Mac or your home folder. Grant **Full Disk Access** to disky in System Settings to include protected folders. You may need to quit and reopen disky after granting it.
4. Click **Scan now**. The menu shows progress; open disky to browse the local result. Scheduling and Open at login are opt-in settings.

The local view is the default. Selecting disk.rbw.sh only changes the viewer; uploading requires a separately configured destination. This pilot does not automatically upload your files or scan metadata.

## M1 test checklist

- Fresh install opens local mode and Settings; no existing configuration or development tools are required.
- Grant Full Disk Access, scan your home folder, then scan the whole Mac. Record errors and processing time.
- The home-folder shortcut and window title identify this Mac and this user.
- Open a directory, search, change the age lens, and inspect the table.
- Run a second scan after changing a small test folder; inspect its diff. Allow at least a minute between scan starts: current snapshot IDs have minute precision, so same-minute runs replace that minute's snapshot.
- Enable scheduling and Open at login. Verify a scan after sleep/wake and a launch after logout/login.
- Replace the app with the next pilot and verify saved settings, prior scans and agent execution. Ad-hoc builds may need Full Disk Access granted again after replacement.
- Disable scheduling and Open at login before removing the app.

Scans are stored in `~/Library/Application Support/disky/scans` (newest eight retained by default). Settings are in `~/.config/disk-tree/disky.json`; logs are in `~/Library/Logs/disk-tree`.

Sizes count allocated bytes per path. APFS clones and hardlinks can share blocks, so a subtree's size can exceed what deleting it would free. Full Disk Access does not grant root privileges; root-only folders remain unreadable. Independent sources, mounted volumes and SSH/NAS schedules are the next milestone.
