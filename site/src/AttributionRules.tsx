import { useEffect } from 'react'
import { useStore } from './store'
import { userBytes, type TreeNode } from './types'

/** Where owners come from, and where to go with questions. Assignments are the
 *  source of truth; inferred ownership is only the bootstrap for whatever
 *  nobody has assigned yet — so this stays short and points at people, not at
 *  the rule tables (those live in the deployment's identity map). */
export function AttributionRules({ tree }: { tree: TreeNode }) {
  // deep-linkable: the section mounts after data loads, so honor #attribution then
  useEffect(() => {
    if (location.hash === '#attribution') document.getElementById('attribution')?.scrollIntoView()
  }, [])

  const { contact } = useStore()
  const attributed = userBytes(tree)
  const pct = ((100 * attributed) / tree.b).toFixed(1)
  return (
    <section className="attrib" id="attribution">
      <h2>Ownership</h2>
      <div className="prose">
        <p>
          An owner comes from one of two places. An <b>assignment</b> — someone assigning a prefix to a person
          (themselves or anyone else) from the map, the CLI, or the API — is the source of truth and always wins.
          Everything unassigned falls back to
          <b> inferred</b> ownership (W&amp;B run configs, <code>.executor_info</code> sidecars, provenance records,
          and a short list of manual prefix rules): “this user's runs
          wrote these bytes” — a starting point for finding your data, not a bill. Ownership has one axis: a
          person, or nobody. <b>{pct}%</b> of bytes have an owner today; the rest shows as{' '}
          <i>unowned</i> (gray) until someone assigns it — shared corpora and infra included, because a
          deletion needs a person to sign off.
        </p>
        {contact && (contact.email || contact.chat) && (
          <p className="feedback">
            Questions, a wrong owner, access for a teammate:{' '}
            {contact.email && <a href={`mailto:${contact.email}`}>{contact.email}</a>}
            {contact.email && contact.chat && ' or '}
            {contact.chat && <a href={contact.chat.href} target="_blank" rel="noreferrer">{contact.chat.label}</a>}.
          </p>
        )}
      </div>
    </section>
  )
}
