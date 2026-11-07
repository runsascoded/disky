import { expect } from 'vitest'
import * as matchers from '@testing-library/jest-dom/matchers'

declare module 'vitest' {
  interface Assertion<T> extends matchers.TestingLibraryMatchers<unknown, T> {}
  interface AsymmetricMatchersContaining extends matchers.TestingLibraryMatchers<unknown, unknown> {}
}

// Bind to this package's runner, not the workspace's hoisted Vitest version.
expect.extend(matchers)

// jsdom doesn't ship a ResizeObserver; the Treemap uses one to track its
// container size. Stub it out so component tests don't crash.
class MockResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
if (typeof globalThis.ResizeObserver === 'undefined') {
  (globalThis as unknown as { ResizeObserver: typeof MockResizeObserver }).ResizeObserver = MockResizeObserver
}
