# Shared chart and row-tooltip fixes ready for CW review

Status: 2026-10-11 root disky uncommitted implementation. Ryan reported detached-looking labels in the CW stacked size chart and a trash tooltip covering preceding table rows. No production deployment was performed.

The root checkout's `packages/react/src/TimeSeries.tsx` now puts a stacked band's own-size labels just inside its upper trace rather than at its vertical midpoint. Total labels remain above the stack. Bands too thin for a label, overlapping labels, and labels whose trace is above a fitted viewport are suppressed rather than floated onto a viewport edge. Tests pin those coordinates. This fixes a confusing intentional placement, not incorrect underlying sizes.

The root checkout's `site/src/ChildrenTable.tsx` and `MatchActions.tsx` prefer `placement="right"` for row trash tooltips. Shared Floating UI machinery already flips to the opposite side and constrains width to the viewport. Other tooltip defaults stay unchanged. Exact markup/placement tests cover filtered and unfiltered actions.

Review/apply the specific root uncommitted diffs with their tests, preserving unrelated CW work. There is no root commit to cherry-pick yet; do not assume merging `cloud` imports these changes. Relevant tests are `packages/react/tests/TimeSeries.test.tsx`, `site/src/childrenTableFilter.test.ts`, and `site/src/MatchActions.test.ts`.

Root CIC used live shared components with synthetic data at `http://127.0.0.1:3269/tmp/cw-ui.html`. This verifies rendered placement but is not an actual CW integration/deployment test. Recheck the actual CW dev root stacked chart and a nested table's trash tooltip, including an action near the viewport edge, before deployment. Do not click a real trash action merely to open its tooltip; use hover or keyboard focus.
