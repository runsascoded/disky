import { useEffect } from 'react'
import { userBytes, type TreeNode } from './types'

const GROUP_EMAIL = 'marin-gcs-usage@openathena.ai'
const DISCORD_URL = 'https://discord.com/channels/1354881461060243556/1412294350645493840'

/** Where owners come from, and where to go with questions. Assignments are the
 *  source of truth; inferred ownership is only the bootstrap for whatever
 *  nobody has assigned yet — so this stays short and points at people, not at
 *  the rule tables (those live in the deployment's identity map). */
export function AttributionRules({ tree }: { tree: TreeNode }) {
  // deep-linkable: the section mounts after data loads, so honor #attribution then
  useEffect(() => {
    if (location.hash === '#attribution') document.getElementById('attribution')?.scrollIntoView()
  }, [])

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
        <p className="feedback">
          Questions, a wrong owner, access for a teammate:{' '}
          <a href={`mailto:${GROUP_EMAIL}`}>{GROUP_EMAIL}</a> or{' '}
          <a href={DISCORD_URL} target="_blank" rel="noreferrer">#internal-discuss</a> on the Marin Discord.
        </p>
      </div>
    </section>
  )
}
