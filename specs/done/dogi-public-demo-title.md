# Public R2 demo title and predictable image shortcut

The public demo uses Ryan's title, “Public R2 Treemap Demo” (with “(dev)” on the preview deployment). Root cards draw an identical site/view title once and reclaim 32 pixels for the map. Drill cards retain the site eyebrow and folder title; private card policies are unchanged.

`/og?c=t` and `/og/?c=t` redirect temporarily to `/og/map.png?c=t`, preserving all query parameters and the same origin. The shortcut has `Cache-Control: no-store`; the existing canonical renderer retains responsibility for authorization, redaction, scan selection, and image caching. Without a scan parameter, it renders the latest scan. Stamped page metadata continues to advertise a scan-pinned, renderer-versioned canonical image rather than the convenience shortcut. The unrelated page parameter `card=4` is only a caller's cache-buster, not a snapshot or renderer version.

The renderer revision is 5. An exact text-node/geometry regression first reproduced the duplicate title and now verifies a single 32-pixel heading. Shortcut tests verify exact destinations, preserved view/signature parameters, and uncached redirects.

Live preview revealed that Pages dispatched the bare shortcut to the pre-existing catch-all, despite the index route. Follow-up `c56a1525` handles `/og` and `/og/` at that catch-all; its additional tests reproduced the 404 before the fix. Adopt this together with shared heading/shortcut commit `1211feaa`.

Verified rollout on 2026-10-13: public release `f4943d93`, preview CI [38406251675] and production CI [38406775181] succeeded. The release site has 699 passing tests and one skipped; shared OG tests have 88 passing. Root and bounded release builds/typechecks succeeded. Production `/og?c=t&card=5` returns a 302 to the same-origin PNG and then HTTP 200, 1200×630; Chrome verification shows one “Public R2 Treemap Demo” heading. The page advertises `/og/map.png?c=t&r=5&s=2026-10-13`.

Cache-policy follow-up: the unpinned PNG's live Cloudflare response advertises `public, max-age=14400, must-revalidate`, even though the origin renderer sets zero max-age for unpinned images. The shortcut itself is uncached, but the redirected image can lag a new scan by four hours under this edge policy. Use the stamped scan-pinned image for reproducible presentation snapshots; investigate the Cloudflare cache override before promising instant daily refresh at the unpinned image URL.

[38406251675]: https://github.com/runsascoded/disky/actions/runs/38406251675
[38406775181]: https://github.com/runsascoded/disky/actions/runs/38406775181
