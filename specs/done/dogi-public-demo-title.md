# Public R2 demo title and predictable image shortcut

The public demo uses Ryan's title, “Public R2 Treemap Demo” (with “(dev)” on the preview deployment). Root cards draw an identical site/view title once and reclaim 32 pixels for the map. Drill cards retain the site eyebrow and folder title; private card policies are unchanged.

`/og?c=t` and `/og/?c=t` redirect temporarily to `/og/map.png?c=t`, preserving all query parameters and the same origin. The shortcut has `Cache-Control: no-store`; the existing canonical renderer retains responsibility for authorization, redaction, scan selection, and image caching. Without a scan parameter, it renders the latest scan. Stamped page metadata continues to advertise a scan-pinned, renderer-versioned canonical image rather than the convenience shortcut. The unrelated page parameter `card=4` is only a caller's cache-buster, not a snapshot or renderer version.

The renderer revision is 5. An exact text-node/geometry regression first reproduced the duplicate title and now verifies a single 32-pixel heading. Shortcut tests verify exact destinations, preserved view/signature parameters, and uncached redirects.
