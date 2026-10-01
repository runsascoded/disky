# Avatar sources: GitHub → Google → Discord

Shipped v1 (`Avatar.tsx`): GitHub photo via `github.com/<handle>.png`, handle
guessed from the canonical id / email local-part (`whoToHandle`), colored-initial
fallback on 404. Good coverage because canonical ids are GH-username-shaped — but
it's a *guess*, and it's only GitHub. This spec makes it deterministic and
multi-source, per the requested priority **GitHub > Google > Discord**.

## Make handles explicit in `identities.yaml`

The guess fails for anyone whose canonical id ≠ their GH login (W&B-only ids,
mismatched spellings). Add optional per-user handle fields, exported through
`rules.json` by `gcs-usage rules`:

```yaml
users:
  will-held:
    team: oa
    aliases: [wheld]
    github: WillHeld          # → github.com/WillHeld.png
    google: will@openathena.ai # → Gravatar / Directory pfp
    discord: "1093…"          # Discord user id → cdn.discordapp.com/avatars/…
```

All optional; absent → fall back to the guess, then initials. Curating ~31 users
is a one-time pass (GH logins are the high-value 90%).

## Resolution order (per user, first that yields an image wins)

1. **GitHub** — `github.com/<github ?? guessed-handle>.png`. Free, no auth. Done.
2. **Google** — CF Access's identity endpoint does **not** expose the Google pfp, so
   there's no free path. Options: (a) **Gravatar** `gravatar.com/avatar/<md5(email)>?d=404`
   — works only if the user set one (rare), zero infra; (b) Google **Directory API**
   with a workspace service account — real photos for all `@openathena.ai`, but
   admin creds + a server endpoint to proxy/cache them. Start with Gravatar-404;
   add Directory only if demand warrants.
3. **Discord** — needs the user's Discord id + avatar hash → `cdn.discordapp.com/avatars/<id>/<hash>.png`.
   We have the Discord archive (see [[discord-archive-access]]); a one-time
   member-list pull maps display-name/email → id + current avatar hash, cached.
   Lowest priority.

Client tries them in order via `<img onError>` chaining (or a resolved-URL list
computed once), ending at the colored initial.

## Also wire avatars into

- the **user legend / attribution rollup** (the "by user" list — canonical ids, so
  no guess needed) with the same hover card;
- the **mark provenance** tooltips and the header **whoami** chip (already an
  initials avatar — swap in `Avatar`).

## Notes

- External image loads leak "who is being viewed" to GitHub/Discord/Gravatar and
  add third-party dependencies. Acceptable for an internal gated dashboard; if not,
  proxy + cache avatars through a Function (also fixes Google Directory auth).
- Keep the initials fallback first-class — it's the privacy-preserving default and
  covers everyone without a handle.
