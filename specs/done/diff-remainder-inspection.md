# Inspecting grouped diff cells

The diff's `(other)` cells are synthetic remainders: each expanded folder's before/after totals less the children drawn individually. A large collection of small entries can therefore form a large cell. A normal click opens its actual containing folder while preserving the selected scan range. An ⌥-click pins its combined totals; the inspector explains the grouping and retains an explicit **Open containing folder** action. The folder's own diff can expose more individual children at the narrower scope.

When the containing folder is already the comparison scope, a normal click scrolls to the existing listing; the pinned inspector also offers **View folder listing**. The inspector explicitly states that this is the after-scan listing, including entries outside the group; it does not claim to list the exact historical remainder membership. Deleted entries require the earlier scan's listing. Unchanged filler cells explain their distinct meaning.

Only the reserved `(other)` label and explicit filler status receive synthetic treatment. Real names such as `(experiment)` and `(notes).txt` drill or open normally. The controlled diff map remounts when its scope or scan pair changes, clearing stale pinned cards and their relative actions.

Remote Cloud Build `8f857fae-a48c-4629-ad2c-d1590d79546e` passed 22 focused tests, both TypeScript checks and the production build. Browser verification exercises the reported CW comparison with read-only live API responses in isolated Chrome; the shared browser connector was unavailable because its profile was locked. No raw object data is fetched or modified by this treatment.

Deployed to CW dev (`ad5e028e`) and production (`a97e551f`) on 2026-11-09. Final build/type checks include the scope/pair pin reset added during browser verification. The reported grouped cell opens its containing folder, successive drills reach the checkpoint files, and the date-range parameter remains unchanged. At a 390px viewport the inspector has no internal clipping or document-width overflow; the listing action scrolls to the actual listing. Browser verification reported no page errors.

The direct-click follow-up on 2026-11-09 removes the extra pin-then-button step. Normal clicks navigate, ⌥-click pins, and actionable cells show a pointer cursor. Browser verification followed successive real remainder cells to the file listing, checked the unchanged date range, and confirmed mobile listing navigation with zero page errors. Cloud Build `d4d3c060-5db1-450a-a9ba-2f885016b307` passed 69 focused navigation, activity and session-logging tests, both TypeScript checks and the production build.

Released to CW production (`23dd3167`) and dev (`28446fd5`). No scan-source, IAM, object-data or history-pointer changes were included.

The density-grid follow-up gives server-created diff remainders the same geometric cross-hatch as renderer-folded cells. `CellStyle.dustCount` carries the optional density hint through DOM and canvas painting, including the canvas export path. Diff remainders use the larger before/after object count; this is approximate density, not an asserted direct-child count or measured size distribution. Unchanged filler has no reliable count and remains untextured. The synthetic-cell predicate lives in the pure diff model so server-side digest rendering does not import frontend types.

Cloud Build `25339ba3-258d-4b6a-9dd5-e3836a940cbc` passed 261 focused tests, both TypeScript checks and the production build. Isolated Chrome verified painted grid pixels, DOM and canvas screenshots, preserved click-through range and mobile listing navigation. The actual Slack preview page also rendered the companion quiet-bucket grouping with synthetic fixtures and zero browser errors.

The density grid is deployed to production `2e8c3d34` and dev `7b3fb4f5`. Live production Chrome verification confirmed painted grid pixels and direct navigation with the comparison range preserved.
