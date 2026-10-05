# Migrate legacy SMAppService agents to plain LaunchAgents

From the m3 deployment audit, 2026-10-05. Target: `wt/app`.

## Observed failure

The installed app already uses the plain-agent implementation in `apps/tauri/src-tauri/src/services.rs`, but both loaded services (`com.runsascoded.disky.scan` and `.drain`) were still legacy SMAppService jobs: launchd showed a relative `Contents/MacOS/disky` program, a path submitted by `smd`, version 0.1.1, exit 78, and a launch-constraint / LWCR failure. The on-disk `~/Library/LaunchAgents/` plists had correct absolute programs. The last successful scheduled capture was 2026-10-03 18:10 EDT; the D1 heartbeat was stale too.

Running the installed app's `disky agents register scan` and `disky agents register drain` repaired both services without changing FDA or config. `disky scan now` completed a whole-machine capture and automatic Batch ingest; D1 heartbeat resumed. We have not rebooted, so recurrence from legacy BTM registration is a hypothesis to verify, not a proven cause.

## Requested behavior

On upgrade/startup, detect and explicitly unregister legacy `SMAppService.agentServiceWithPlistName` registrations for these two agents before loading the plain plists. Preserve the separate `SMAppService.mainApp` login item, user scheduling/scope settings, credentials, and disabled-agent intent. An enabled plain plist with a loaded legacy or failed service must not be reported simply as healthy/enabled.

Inspect whether the existing register command can safely perform this migration automatically, and ensure repeated migration is a no-op once complete. Surface actual scan/drain launch failures in app status. Avoid blindly enabling services the user disabled.

## Verification

Cover the legacy-to-plain transition, already-plain state, and disabled state with precise tests around the service adapter. Verify launchd uses the absolute installed app binary, the scan runs with FDA coverage, and the drainer heartbeat resumes. Check a rebuild and logout/login or reboot when Ryan authorizes that disruption; do not claim persistence based only on the current successful bootstrap.

## Implementation in the app pilot

GUI startup migrates only agents whose plain plist exists, unregistering legacy SMAppService state before replacing an outdated/unloaded plain agent. Explicit register and unregister commands also clear legacy registrations. Repeated startup with an already current plain agent makes no changes. The main-app login item is untouched. An active job defers migration instead of being killed; the log requests reopening after the job finishes.

Enabled intent is determined by the installed plist, separately from launch health. Settings reports each agent's health, and the scan menu reports stale programs, unloaded plists and nonzero last exits. `agentctl install` now preserves the individual scan/drain plists even when their services are unhealthy, rather than inferring enabled intent from a successful status string.

Adapter tests cover legacy, already plain, disabled, missing/stale loaded programs, failed launches and an active job. These tests do not manipulate the user's launchd domain. Real installed-app FDA, scheduled scan/drainer heartbeat and reboot/logout persistence still require deployment verification on m3; the pilot build does not replace its installed app.
