# dOGI color-mode parity

Status: 2026-10-13. Shared `cloud` fix implemented and verified; public R2 and OA R2 rollouts are live. GCS/CW handoffs remain with their deployment owners. No new bindings, permissions or infrastructure are required.

The map-card renderer previously retained `c` in URLs/cache identities but ignored it, drawing every card with owner colors. It now shares the interactive map's URL codec and available-axis fallback. `c=t` / `tree` uses the existing size-ranked directory palette and child hue fan; `c=w` / `written` / `date` / `age` uses the existing date gradient over the card's drawn leaves; `c=r` / `read` uses the scan metadata's access-log observation window, with brick-red cells for missing observations; `c=u` / `user` retains owner colors. Folded remainders stay neutral. Tree legends name directories; date/read cards do not show a misleading owner legend. Subtitles identify the selected effective axis, including provider-aware created/modified terminology.

Absent/invalid color selections default to owner on attributed scans and tree otherwise. A deployment whose frontend config sets `Store.defaultColor` must also set `OG_DEFAULT_COLOR` to match (`w` for OA R2's written default). Explicit URL selections win. Unsupported user/read axes fall back to owner if available, otherwise tree, using the same helper as the interactive dashboard.

The render version advances to `r=3` so new stamped URLs and internal caches cannot reuse the old palettes. External card consumers may still retain previously fetched unfurls; re-fetch the page metadata to obtain the new image URL. The [preview privacy contract] is unchanged: private generic cards never reach the map renderer; GCS must retain its explicit `OG_ANONYMOUS=shape` setting during rollout if its approved geometry/totals policy should remain.

Validation: nine exact regression cases failed before the fix; aliases, metadata defaults, missing read coverage, configured defaults, axis-specific legends and URL/cache propagation pass afterward. The real SVG/resvg-WASM pipeline generates four distinct PNGs from identical synthetic geometry. Full site suite: 1,931 passed / one skipped; client and Functions typechecks plus production build pass. Chrome verification: [local color-mode comparison]. Production verification remains the deployment owner's responsibility.

## Deployment rollout

The public R2 deployment was 344 commits behind shared `cloud`, so it received a bounded backport on `r2-og-release`, starting from its deployed `e1db595e` baseline. `042dc8bf` enables public dynamic metadata without requiring `SESSION_SECRET`, preserves the original credential view separately from scan/render metadata, versions cards with `r=3` and the scan ID, and honors the same color codec on the dashboard and cards. Both dev and production CI passed; live HTML and real PNGs were verified in Chrome at [public tree view] and [public written view]. The observed scan was October 13: 973 GiB / 1,393,555 objects, with distinct palettes and identical geometry/totals. This is not a bulk architecture upgrade: the root shared `cloud` branch was intentionally not pushed to the public deployment branch.

`a1d96f6c` backports the existing provider-aware listing timestamp wording from `f433df9e`, after live checking exposed the older dashboard's incorrect “created” wording for R2's `LastModified`. Local validation: 688 tests passed / one skipped, client and Functions types and production build passed. Both [timestamp preview CI] and [timestamp production CI] passed. Production Chrome verified “modified” in the table, treemap details, and listing-time explanation, with “written” still selected. No timestamp values or index formats change.

Deployment-owner handoffs were delivered to GCS and CW. OA R2 reports the color fix deployed to dev/prod, retaining generic anonymous privacy and its written default. CW acknowledged the rollout and is waiting for its first isolated scheduled scan gate. GCS has not yet acknowledged; its deployment must explicitly retain `OG_ANONYMOUS=shape` before integrating the shared privacy changes.

Follow-up `r=4`: Ryan's live unfurl exposed fractional HSL components rendering black, missed by the initial two-child/integral-hue fixture and dimension-only checks. Shared `500e55d4` normalizes card HSL fills via a DOM-free core parser, adds exact seven-shade real-WASM pixel assertions, and labels explicitly public maps without changing their unsigned tier or private policies. Public R2's bounded `1e6d60d2` passed [RGB preview CI] and [RGB production CI]. Chrome verified actual tree/written PNGs in dev and the labeled, correctly colored tree PNG in prod; production metadata advertises `r=4` and the improved product description. The [fresh public tree unfurl] uses an otherwise ignored parameter to distinguish it from consumers' cached old page URLs. See [SVG color follow-up] for cause, test coverage, and disclosure boundaries.

[preview privacy contract]: dogi-privacy.md
[local color-mode comparison]: http://127.0.0.1:3269/tmp/og-color-preview.html
[public tree view]: https://r2.rbw.sh/?c=t
[public written view]: https://r2.rbw.sh/?c=w
[timestamp preview CI]: https://github.com/runsascoded/disky/actions/runs/38395163362
[timestamp production CI]: https://github.com/runsascoded/disky/actions/runs/38395810383
[RGB preview CI]: https://github.com/runsascoded/disky/actions/runs/38401939974
[RGB production CI]: https://github.com/runsascoded/disky/actions/runs/38402673967
[fresh public tree unfurl]: https://r2.rbw.sh/?c=t&card=4
[SVG color follow-up]: dogi-svg-colors.md
