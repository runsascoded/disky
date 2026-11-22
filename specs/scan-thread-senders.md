# Scan threads: status as the sender (icon + name), green/red streak threads

From the r2 session, 2026-11-21: Ryan's design for `#<deploy>-admin`'s scan thread (`nominal`) and the deploy notices. Every message today repeats "R2 Usage Bot" + the app icon, which tells the reader nothing, and the status is an emoji at the start of the text. Slack fixes a message's `username` / `icon_url` when it's posted (`chat.update` can't change them), so the sender has to carry only what never changes for that message.

## Design (Ryan's)

1. **Threads are streaks of one kind.** A clean (green) streak's head and a failing (red) streak's head each post with a fixed sender:
   - icon: a ✅ / ❌ image (any image works: a badge, a mosaic),
   - name: the kind, e.g. `OA R2 scans · clean` / `OA R2 scans · failing` (no counts, nothing that changes; ≤ 80 chars).
   The head's text is still edited each run (count, since, latest, stores, "ended by …"), but drops the leading `✅ OA R2 scans:` that the sender now says.
2. **A scan that starts** posts a ⏳ reply (⏳ sender icon) in the current thread.
3. **A scan that finishes** deletes its ⏳ reply, then:
   - **same kind as the thread** (clean run in a green thread, failed run in a red one): re-posts it as a finished reply whose sender icon is the outcome (✅ / ❌; optionally the starter's face with the outcome as a badge, as `_lib/badgedAvatar.ts` does for staged deletions), so the text loses the inline status emoji and `:ryan:` face;
   - **kind flipped**: nothing more in the old thread. The run's canonical message is the new thread's head (the other kind), which closes the old thread as today (`closed_head`).
4. **Step alerts** (grouped) stay replies, posted once in their final state, so their icon can be the alert's kind (⚠️ / ❌).
5. **Deploy notices** (`deploy_notify`, the `OA-R2 dev deployment succeeded` thread): the status emoji (✅ / 🚀 / 💥) moves from the name into the icon; the name keeps the words.

## With quiet greens (#85, Ryan 2026-11-22: quiet greens on by default, every deployment)

#85 came after this design: a clean thread posts its head and one rolling reply (`trail`), then runs only edit them (`nominal.quiet`, default on); a red notice or unclean run posts as before. The two compose:

- **Senders everywhere they post:** a green head (✅ icon, `… · clean`), a red head (❌, `… · failing`), the rolling reply (a fixed "runs" sender, e.g. 🧾 / `… · runs`: its text changes, its sender can't), alerts (⚠️ / ❌), deploy notices (emoji in the icon).
- **Green threads under `quiet` (the default):** no ⏳ reply and no per-run re-post (§2–3 above); a run edits the head and the rolling reply only. A run that flips the kind posts the new red head (§3, "kind flipped").
- **Red threads, and green ones with `quiet: false`:** §2–3 as written: a ⏳ reply, deleted and re-posted as the finished reply with the outcome as its icon.
- **Deploy notices under `deploy_quiet`:** the green head is edited (its sender stays 🚀 / 🧪); reds post with their own sender.

## Things to settle in the implementation

- **`thread_url`** (`scan_runs.thread_url`, just added): set when the finished reply is posted (the ⏳ reply's link dies with it); for a flipped run, the new head's permalink.
- **Delete + re-post under the thread lease**, so a `batch-events` converge and the job's own end can't both re-post. A head that dies without its end (cancel, lost VM) is converged by `batch-events serve`, which must do the same delete + re-post. Deleting a reply with no replies of its own leaves no tombstone.
- **Icon hosting:** `icon_url` must be public. Options: the deployment's icons Pages project (`icons_base`), or the site's public `/a/<sha256>.<ext>` (`public_avatars`) for faces with badges.
- **Recovery / adoption** (`adopt`, `owns`, `find`): messages become `bot_message`s with per-message usernames; ownership checks must still recognize them (bot id, not username).
- **Every deployment** gets it (parity); the r2 thread in `#r2-admin` is a fine first one to look at.

<!-- Claude-Session: https://claude.ai/code/session_01MJgqTbCPPaPpxRYa9ujuVB -->
