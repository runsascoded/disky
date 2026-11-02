import { describe, expect, it } from 'vitest'
import { tokenize } from './mrkdwn'

describe('the OP markdown, tokenized', () => {
  it('a headline: arrow, bold, links, plain text', () => {
    expect(tokenize(':arrow_deg-20: **+1.0 TiB** [month-to-date](https://x/?d=1) · 736 TiB · [dashboard](https://x/)')).toEqual([
      { t: 'arrow', deg: -20 }, { t: 'text', s: ' ' }, { t: 'bold', s: '+1.0 TiB' }, { t: 'text', s: ' ' },
      { t: 'link', s: 'month-to-date', href: 'https://x/?d=1' }, { t: 'text', s: ' · 736 TiB · ' }, { t: 'link', s: 'dashboard', href: 'https://x/' },
    ])
  })
  it('italics, Slack bold, the plot, and an underscore inside a word left alone', () => {
    expect([tokenize('_as of 9/1 08:00Z_'), tokenize('*Weekly summaries*'), tokenize('![S3 usage — September 2026](<plot>)'), tokenize('main_data _(partial)_')]).toEqual([
      [{ t: 'italic', s: 'as of 9/1 08:00Z' }],
      [{ t: 'bold', s: 'Weekly summaries' }],
      [{ t: 'image', alt: 'S3 usage — September 2026', src: '<plot>' }],
      [{ t: 'text', s: 'main_data ' }, { t: 'italic', s: '(partial)' }],
    ])
  })
})
