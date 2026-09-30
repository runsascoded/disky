# Packaging disk-tree as `disk-tree.app` (macOS)

v1 wraps the existing Flask server + built React UI in a native WKWebView window
(`disk_tree.desktop`) and freezes it with **PyInstaller** (py2app needs a *framework* Python; the uv-managed standalone CPython 3.13 is one PyInstaller handles and py2app does not). This gives scans a stable
TCC identity — prompts and Full Disk Access show **"disk-tree"**, and one FDA
grant covers the app's child `gfind`. See `specs/macos-app.md` for the full
design (and the Tauri v2 plan).

## One-time: a stable self-signed signing identity

Ad-hoc signing (`codesign -s -`) works but its cdhash changes every rebuild, so
the FDA grant breaks each time. A **self-signed certificate** gives a stable
identity with no Apple Developer account:

Keychain Access → *Certificate Assistant* → *Create a Certificate…*
- Name: `disk-tree-selfsigned`
- Identity Type: *Self Signed Root*
- Certificate Type: *Code Signing*
- (Let it create; then in Keychain, set the cert to *Always Trust* for code signing.)

Verify it's usable: `security find-identity -v -p codesigning` should list
`disk-tree-selfsigned`. Override the name with `DISK_TREE_SIGN_ID=…`.

No full **Xcode** is needed at any point — only the Command Line Tools
(`xcode-select --install`) for `codesign`.

## Build

```bash
packaging/macos/build.sh      # → dist/disk-tree.app  (builds UI, freezes, signs)
open dist/disk-tree.app
```

## Grant Full Disk Access

System Settings → Privacy & Security → Full Disk Access → add
`dist/disk-tree.app` (or wherever you move it). Now it — and its scans — read
protected folders (Photos/Desktop/Documents/Downloads) with no prompts.

## Runtime dependency: `gfind`

v1 still shells out to GNU find. The bundle sets no PATH of its own, so install
`gfind` (Homebrew: `brew install findutils`) and ensure `/opt/homebrew/bin` is
reachable, or the scan errors. v2 (Tauri + a native `getattrlistbulk` walker)
removes this dependency.

## Debugging

The window is a **WKWebView** (WebKit), not Chrome — so WebKit-only errors (e.g.
`The string did not match the expected pattern`, WebKit's wording for a bad
`URL`/regex) surface here but *not* under the usual Chrome/dev-server check.
There are three layers to look at:

1. **The WKWebView Web Inspector** (JS console, network, sources) — the only way
   to read a JS stack out of the native window. `disk_tree.desktop` enables it
   via `webview.start(debug=…)`: **on by default from source**, and in the
   **frozen bundle** with `DISK_TREE_APP_DEBUG=1`. Then right-click in the
   window → *Inspect Element*. Fastest repro (no 332 MB rebuild):

   ```bash
   uv sync --extra app
   disk-tree-app                 # debug on by default from source
   # right-click → Inspect Element → Console, then reproduce
   ```

   For the installed bundle: `DISK_TREE_APP_DEBUG=1 open dist/disk-tree.app`.

2. **Python / server logs** (Flask + waitress, the scan itself) go to **stderr**.
   Launched via Finder/`open` they land in the unified log — read them with
   `log stream --predicate 'process == "disk-tree"' --level debug`, or just
   launch the bundle's executable from a terminal to see them inline:
   `./dist/disk-tree.app/Contents/MacOS/disk-tree`.

3. **Headless server smoke** (no window): `DISK_TREE_APP_SMOKE=1 disk-tree-app`
   brings the embedded server up, hits `/api/scans`, prints a one-line result,
   and exits — isolates "is the backend fine?" from "is the WebView fine?".

## Notes / known-fiddly

- `disk_tree/static/` is a **build artifact** (copied from `ui/dist`); it's
  git-ignored, not committed.
- PyInstaller + native wheels (pyarrow/duckdb) can need `collect_all`
  tuning in `disk-tree.spec` if the first build reports missing modules.
- For *distribution* to other machines (not just this one), you'd add hardened
  runtime + Developer ID + notarization (`notarytool`) — also CLT-only.
