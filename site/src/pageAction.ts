/** Page titles carry their real URL, which is also an exact search keyword.
 * This keeps navigation discoverable among many dated scan actions. */
export function pageAction(path: string, title: string) {
  return {
    label: `${title} · ${path}`,
    group: 'Pages',
    keywords: [path, path.replace(/^\//, '')].filter(Boolean),
  }
}
