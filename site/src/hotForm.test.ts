import { createElement, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { HotSearchFields, HotSearchForm } from './HotSearch'
import { changeHotDraft, hotDraft, hotDraftParams } from './hotModel'

const render = (query: string) => renderToStaticMarkup(createElement(HotSearchForm, { params: new URLSearchParams(query), onSearch: () => {} }))
const selected = (html: string) => [...html.matchAll(/<select name="([^"]+)"[^>]*>(.*?)<\/select>/g)].map(([, name, options]) =>
  [name, [...options.matchAll(/<option([^>]*)>(.*?)<\/option>/g)].filter(([, attrs]) => / selected=""/.test(attrs)).map(([, attrs, text]) => /value="([^"]*)"/.exec(attrs)?.[1] ?? text)])

describe('URL-authoritative root search form', () => {
  it('supports explicit dated availability without restoring an absent baseline or inventing future options', () => {
    const renderDated = (query: string) => renderToStaticMarkup(createElement(HotSearchForm, { params: new URLSearchParams(query), dates: ['2026-10-04', '2026-10-05', '2026-10-06'], onSearch: () => {} }))
    expect(selected(renderDated('date=2026-10-06&name=AB&from=2026-10-05'))).toEqual([['date', ['2026-10-06']], ['from', ['2026-10-05']]])
    expect(selected(renderDated('date=2026-10-06&name=AB'))).toEqual([['date', ['2026-10-06']], ['from', ['']]])
    expect(selected(renderDated('date=2026-10-05&name=AB&from=2026-10-06'))).toEqual([['date', ['2026-10-05']], ['from', ['2026-10-06']]])
    expect(renderDated('date=2026-10-05&name=AB').match(/<select name="from">(.*?)<\/select>/)?.[1]).toBe('<option value="" selected="">No baseline</option><option>2026-10-04</option>')
  })
  it('renders no baseline when from is absent, even after rendering a comparison URL', () => {
    expect(selected(render('date=2026-10-05&name=.json&from=2026-10-04')))
      .toEqual([['date', ['2026-10-05']], ['from', ['2026-10-04']]])
    const html = render('date=2026-10-05&name=.npy')
    expect(selected(html)).toEqual([['date', ['2026-10-05']], ['from', ['']]])
    expect(html.match(/<input[^>]+>/)?.[0]).toBe('<input required="" aria-describedby="hot-availability" name="name" value=".npy"/>')
    expect(html.match(/<form[^>]+>/)?.[0]).toBe('<form autoComplete="off">')
    expect(html.match(/<label>Baseline scan \(optional\)(.*?)<\/label>/)?.[1]).toBe(
      '<select name="from"><option value="" selected="">No baseline</option><option value="2026-10-04">2026-10-04</option></select>',
    )
  })
  it('uses controlled values and handlers for all fields, not browser-restorable defaults', () => {
    const onChange = vi.fn()
    const fields = HotSearchFields({ draft: { name: '.json', date: '2026-10-05', from: '' }, onChange })
    const labels = fields.props.children as ReactElement<{ children: [string, ReactElement] }>[]
    const controls = labels.map(label => label.props.children[1])
    expect(controls.map(control => {
      const props = control.props as { name: string; value: string; defaultValue?: string; onChange?: unknown }
      return { name: props.name, value: props.value, defaultValue: props.defaultValue, controlled: typeof props.onChange === 'function' }
    })).toEqual([
      { name: 'name', value: '.json', defaultValue: undefined, controlled: true },
      { name: 'date', value: '2026-10-05', defaultValue: undefined, controlled: true },
      { name: 'from', value: '', defaultValue: undefined, controlled: true },
    ])
    controls.forEach((control, i) => {
      const { onChange } = control.props as { onChange: (event: { target: { value: string } }) => void }
      onChange({ target: { value: ['.npy', '2026-10-04', '2026-10-04'][i] } })
    })
    expect(onChange.mock.calls).toEqual([['name', '.npy'], ['date', '2026-10-04'], ['from', '2026-10-04']])
  })
  it('resets every draft field from the navigated URL, including a removed from parameter', () => {
    const edited = changeHotDraft(hotDraft(new URLSearchParams()), 'from', '2026-10-04')
    expect(edited).toEqual({ date: '2026-10-05', name: '.json', from: '2026-10-04' })
    expect(hotDraft(new URLSearchParams('date=2026-10-04&name=AB')))
      .toEqual({ date: '2026-10-04', name: 'AB', from: '' })
  })
  it('clears a baseline made invalid by choosing the earlier scan', () => {
    const draft = hotDraft(new URLSearchParams('date=2026-10-05&name=.json&from=2026-10-04'))
    expect(changeHotDraft(draft, 'date', '2026-10-04')).toEqual({ date: '2026-10-04', name: '.json', from: '' })
    expect(selected(render('date=2026-10-04&name=.json'))).toEqual([['date', ['2026-10-04']], ['from', ['']]])
  })
  it('serializes the chosen baseline and literal, and omits an empty baseline', () => {
    expect(hotDraftParams({ date: '2026-10-05', name: 'ab&c+', from: '2026-10-04' }).toString())
      .toBe('name=ab%26c%2B&date=2026-10-05&from=2026-10-04')
    expect(hotDraftParams({ date: '2026-10-05', name: '.json', from: '' }).toString())
      .toBe('name=.json&date=2026-10-05')
  })
})
