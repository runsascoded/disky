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
  it("the quota line's marks: stock Slack emoji, by shortcode", () => {
    expect(tokenize('*Quotas:* :red_circle: hot 99.5% · :large_orange_circle: main 86.8% · :large_green_circle: z 4% · :tada:')).toEqual([
      { t: 'bold', s: 'Quotas:' }, { t: 'text', s: ' ' }, { t: 'emoji', name: 'red_circle', char: '🔴' }, { t: 'text', s: ' hot 99.5% · ' },
      { t: 'emoji', name: 'large_orange_circle', char: '🟠' }, { t: 'text', s: ' main 86.8% · ' }, { t: 'emoji', name: 'large_green_circle', char: '🟢' }, { t: 'text', s: ' z 4% · :tada:' },
    ])
  })
  it("`runway-hue`'s marks: the workspace's :runway-1: … :runway-8:, by their circle colors", () => {
    expect(tokenize('*Quotas:* :runway-8: main 86.8% · :runway-1: hot 99.5% · :runway-9: x')).toEqual([
      { t: 'bold', s: 'Quotas:' }, { t: 'text', s: ' ' }, { t: 'hue', n: 8, color: '#1e8e4a' }, { t: 'text', s: ' main 86.8% · ' },
      { t: 'hue', n: 1, color: '#e01e1e' }, { t: 'text', s: ' hot 99.5% · :runway-9: x' },
    ])
  })
})
