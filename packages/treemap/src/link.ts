/** The modifier and button state a link click is judged by. */
export type LinkClick = Pick<MouseEvent, 'metaKey' | 'ctrlKey' | 'shiftKey' | 'button'>

/**
 * A click that, on a real `<a href>`, would leave the page alone and open the
 * link elsewhere: ⌘/Ctrl (new background tab), ⇧ (new window), or the middle
 * button. ⌥ is not one: the map's ⌥-click pins.
 */
export const isNewTabClick = (e: LinkClick): boolean =>
  e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1

/**
 * Open `href` the way a link clicked with `e`'s modifiers would, for a surface
 * that can't be an `<a>` itself (a canvas cell, a cell holding nested anchors):
 * a detached anchor gets a synthetic click carrying the new-tab modifier (both
 * ⌘ and Ctrl, whichever this platform reads) and `e`'s ⇧, so the browser picks
 * background tab / foreground tab / new window exactly as for a real link.
 * Called from inside the user's click, so it is not a popup. The click doesn't
 * bubble, so an app's document-level link interceptor (an SPA router) never
 * sees it.
 */
export function openLikeLink(href: string, e: LinkClick): void {
  const a = document.createElement('a')
  a.href = href
  a.rel = 'noopener'
  a.hidden = true
  document.body.appendChild(a)
  const tab = e.metaKey || e.ctrlKey || e.button === 1
  try {
    a.dispatchEvent(new MouseEvent('click', {
      cancelable: true,
      view: a.ownerDocument.defaultView,
      metaKey: tab,
      ctrlKey: tab,
      shiftKey: e.shiftKey,
    }))
  } finally {
    a.remove()
  }
}
