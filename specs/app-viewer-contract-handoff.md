# Bring the app's viewer contract upstream to local

From `wt/app`, 2026-10-05. Target: `wt/local`, then its sibling deployment branches. App release preparation merged `local` at `0c7f34ae` and passes the full site suite (599 passed, one skipped).

Two shared site fixes remain on the app branch: `11d567e9` (`window.__DISKY__` overrides primary-store home/title), and `5b9e7eda` (hide owner assignment and staging controls when the page/store does not support those operations). They are required for a downloaded app on another Mac: a build cannot bake Ryan's home path, and the local Rust server has no plans API. Their callers on the app side are `aa6e7bd2` and `f084f686`.

Integrate the shared fixes into `local` without merging the app branch or its native bundle/deployment files. The import/test context has since changed: the app merge's `site/src/stores.test.ts` keeps `storeQuery`, `withPageConfig` and `TEST_REGISTRY`; PageConfig tests resolve `laptop` through the explicit test registry. Use `git show tauri-native-app:site/src/stores.test.ts` to see the tested reconciled form.

Validation: site build and the complete Vitest suite; verify the injected home/title, read-only local page hiding Staged, and cloud stores retaining their existing write capabilities. Keep cloud write controls scoped to owners/staging capabilities and permission checks. The app fixture at `http://127.0.0.1:7793/~/fixture` has been rendered in Chrome and verified with an exact `one.txt` filter.

Do not move app-specific Python parity tests or native crates into `local` as part of this handoff. The longer-term multi-source reader/store contract is designed at `wt/app/specs/app-source-schedules.md` and will need a separate shared implementation.
