# Shared auth session-log patch

`@open-athena__auth@0.1.0-dist.22b5eae.patch` is a standard pnpm patch applied to the immutable auth dist pin `2c9be8e54a709c8a41e2aab2372cb98e501bdc55`. `package.json` and the frozen lock record it, so clean installs receive the same implementation.

It preserves disky's existing cancellation classification, benign ResizeObserver suppression, navigation-vs-network-failure handling and compact durations; prevents anonymous configuration reads from triggering retention writes; and exposes the existing pure event-summary model without importing React into server code. `auth-session-log.source.patch` contains the corresponding four TypeScript source changes against auth source `22b5eae` for the auth owner's review. It is a handoff artifact, not another install-time patch.

Verification: the full site suite (4,345 tests) and two real Chromium tests passed on the dev node. The latter exercise the actual shared client through its public boot API, including explicit aborts, navigation failures, real network failures and benign browser errors. The route tests cover trusted actor identity, cancellation error counts and retention authorization.

The auth owner has the source patch. Once those fixes have a verified immutable upstream build, update the dependency, remove the matching `patchedDependencies` entry and these patches, regenerate the lock, and rerun the affected auth/client/UI checks. Do not silently drop these behavior checks when updating the pin.
