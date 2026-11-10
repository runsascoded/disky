# Canvas default and shared treemap paint

CW's main-map remainder grids previously lived in a React-only cell overlay, so canvas left those cells flat. The diff already supplied `CellStyle.dustCount`, but its neutral grid ink had poor contrast on colored cells. Both maps now supply density through the shared style: DOM and canvas paint the same geometric grid, with dark ink on brighter fills and light ink on darker fills. Count remains an approximate density cue, not an exact child-size distribution.

Max-mode diff leaves now describe unchanged and changed fill bands as data instead of a CSS gradient. Both renderers paint them. Canvas also positions the existing React cell extras over the bitmap, preserving the change amount and unchanged-byte labels inside those bands. Primary labels, hover, drilling and keyboard overlays continue to use the existing geometry and interaction handlers.

The main map's read and storage-class stripes likewise use structured paint data, rendered as CSS stripes in DOM and line patterns in canvas and PNG export. Canvas resolves browser-supported CSS Color 4 fills, including class-shading color mixes with nested theme variables, to cached sRGB colors.

Canvas is the site's default when no renderer choice is saved. Explicit DOM or canvas choices remain respected. DOM stays available in the settings. The old claim about missing actor badges was stale: CW does not draw those on cells, and the tooltip no longer cites them as a reason to prefer DOM.

Browser checks exercise the reported checkpoint folder in DOM and canvas, plus a fresh browser with no saved preference. Synthetic max-mode data verifies both internal band labels without changing production data. The browser connector's shared profile was locked, so these checks use isolated Chrome with read-only API requests and blocked activity writes.

Released on 2026-11-10 UTC to production Pages `6e5993b4` and dev Pages `17449395`. Cloud Build `273670d1-78c0-4964-ab8c-ff5ddb850fdd` passed 264 tests, both TypeScript checks and the production build. Browser checks found no page errors, verified both max-mode band labels in all three preference cases, and checked actual pixels for stripe patterns and a CSS color mix. The rollout changes site assets only; the scan image, source coverage and history pointers stay as previously deployed.
