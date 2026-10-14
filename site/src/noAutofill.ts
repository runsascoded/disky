/** Spread on a text input that is not a sign-in field (a filter, a "who is this link for" email, a table's
 *  add-row): password managers otherwise read an `email` / `user` / `name` label, name or placeholder as a login
 *  form and offer saved logins (1Password's "Sign in with GitHub" for other `*.oa.dev` sites). `autoComplete`
 *  covers the browser's own autofill; the `data-*` opt-outs are 1Password's, LastPass's and Dashlane's. */
export const NO_AUTOFILL = {
  autoComplete: 'off',
  'data-1p-ignore': true,
  'data-lpignore': 'true',
  'data-form-type': 'other',
} as const
