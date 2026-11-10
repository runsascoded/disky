# Omnibar navigation in a new tab

Status: ready downstream handoff, 2026-11-10. Ryan asked again from the GCS `/meta` omnibar whether ⌘⏎ can open its selected CW navigation item in a new tab. The upstream [use-kbd spec] already describes this feature; it is still untracked/pending there, and the inspected source (`f20b8ce`) has no action `href` or modifier-aware execution support. Disky's pinned dist is `9db5013f5e4034a14530dff37f1c015dc37c8c77`. No new-tab fix is deployed by this handoff.

## Upstream prerequisite

Implement the existing upstream spec in the use-kbd project: a lazily resolved action URL, real activation event propagation through the built-in, headless and cmdk omnibars, normal navigation on Enter, platform-modifier Enter and modified/middle clicks opening a new tab, and a visible shortcut hint only for navigable results. Keep non-navigation actions' handler semantics intact. Resolve the URL synchronously inside the activation gesture so popup blocking is not introduced. Verify ordinary and modified activation once each, including remote link entries, lazy/current URL values and handler-only actions. Publish a verified dist SHA and return it to disky; no per-app keyboard interception workaround is needed.

## Disky adoption

In `site/src/SiteKbd.tsx`, give page/store/user navigation its real destination and retain the router handler for ordinary SPA activation. Give peer and prod/dev switches lazy URLs using the existing `peerHref`/`otherHostUrl` helpers at activation time; these must preserve the current path/query/hash according to those helpers, including `/meta`, scan selection and view parameters. Do not snapshot `location.href` only at registration. Respect each destination's existing access/visibility gates.

Remove duplicate visible “↗” prod/dev results after the one result supports modified activation. Preserve `g shift+d` as a hotkey-only new-tab action if the library supports that visibility distinction; otherwise keep it explicitly until the shortcut can be preserved without duplicate results. A platform modifier must not turn destructive, sharing, sign-out or other handler-only actions into invented URLs.

Folder/file search currently supplies callbacks rather than canonical URLs. Its eventual new-tab support must reuse the app's real leaf/drill URL builders and selected scan/store semantics; do not reconstruct those URLs independently as part of a dependency bump. This does not block the peer/page navigation requested here.

## Verification and rollout

Pin the released dist and update the lockfile through pnpm. Test exact destinations for peer/prod/dev/page actions, current query/hash changes and preserved normal router behavior. Run the upstream modifier tests and disky's affected tests/typecheck on the dev node. Browser-check the actual peer result from `/meta`: Enter navigates the current tab; ⌘⏎ opens the identical destination in another tab, closes the palette, and leaves the original URL unchanged. Check the corresponding Ctrl⏎ behavior on a non-mac platform. Carry the shared adoption to the deployment owners; source adoption and actual deployed verification remain separate.

[use-kbd spec]: /Users/ryan/c/js/use-kbd/specs/omnibar-mod-enter-new-tab.md
