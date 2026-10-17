import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { setRegistry } from './identities'
import { buildRegistry } from './identityRegistry'
import { OwnerFactChip } from './OwnerFactChip'

const chip = (props: Parameters<typeof OwnerFactChip>[0]) => renderToStaticMarkup(createElement(OwnerFactChip, props))

/** The provenance marks a chip renders: each `.mark`'s classes, and whether it holds the W&B logo (an `<svg>`) or text. */
const marks = (html: string) => [...html.matchAll(/<span class="(mark [^"]*)"[^>]*>(<svg|[^<]*)/g)].map(([, cls, body]) => [cls, body === '<svg' ? 'svg' : body])

describe('OwnerFactChip marks', () => {
  beforeEach(() => setRegistry(buildRegistry([
    { u: 'jane-doe', aliases: ['jane', 'jane.doe@example.org'], github: 'janedoe' },
    { u: 'lee-roe', aliases: [] },
  ])))
  afterEach(() => setRegistry({}))

  it("an assignment by someone else carries the assigner's avatar; a self-assignment carries none", () => {
    expect([
      marks(chip({ who: 'jane-doe', assigned: { by: 'lee-roe', ts: 0 } })).map(([c]) => c),
      marks(chip({ who: 'jane-doe', assigned: { by: 'jane.doe@example.org', ts: 0 } })),
    ]).toEqual([['mark by'], []])
  })
  it('a W&B source shows the W&B logo; other sources their glyph', () => {
    expect([
      marks(chip({ who: 'jane-doe', inferred: ['wandb-run', 'marin/proj/abc123', 'marin-us-east1/x'] })),
      marks(chip({ who: 'jane-doe', inferred: ['user-prefix', null, 'marin-us-east1/jane'] })),
    ]).toEqual([[['mark src src-wandb-run', 'svg']], [['mark src src-user-prefix', '/']]])
  })
})
