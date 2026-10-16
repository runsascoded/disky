//! `disky://` links beyond the sign-in hand-off (`applink.rs`):
//!
//! - `disky://view?url=<page>`: open a page of a known site (the configured
//!   one, prod, dev, or this Mac's own) in the app's window.
//! - `disky://reveal?path=<path>`: select the item in a Finder window.
//! - `disky://open-file?path=<path>`: open it in its default app.
//!
//! `path` is absolute (`/Users/…`), store-relative (`Users/…`, the laptop
//! store's root being `/`), or `~/…`.
//!
//! Any web page can navigate to a `disky://` link (the browser asks once, and
//! not again after "always allow"), so a link never proves who sent it. The
//! contract: nothing a link does can run code without a native confirmation.
//! Revealing runs nothing. Opening is immediate only for documents and media
//! whose default app just displays them (`SAFE_EXTS`) and for plain folders;
//! anything else (apps, packages, scripts, installers, HTML, unknown types) asks
//! first in a dialog whose default is "Show in Finder". The type is judged on
//! the resolved target, so a symlink named `x.jpg` to an app still asks.

use std::path::{Path, PathBuf};
use std::process::Command;

use tauri::Url;

#[derive(Debug, PartialEq)]
pub enum Link {
    View(Url),
    Reveal(PathBuf),
    OpenFile(PathBuf),
}

/// `deep` as one of these links; `Ok(None)` for another `disky://` link (the sign-in hand-off).
pub fn parse(deep: &Url) -> Result<Option<Link>, String> {
    if deep.scheme() != "disky" {
        return Err(format!("not a disky:// link: {}", deep.scheme()));
    }
    let param = |k: &str| deep.query_pairs().find(|(n, _)| n == k).map(|(_, v)| v.into_owned()).ok_or_else(|| format!("no `{k}` parameter"));
    Ok(Some(match deep.host_str() {
        Some("view") => Link::View(Url::parse(&param("url")?).map_err(|e| format!("bad url: {e}"))?),
        Some("reveal") => Link::Reveal(path_param(&param("path")?)?),
        Some("open-file") => Link::OpenFile(path_param(&param("path")?)?),
        _ => return Ok(None),
    }))
}

fn path_param(raw: &str) -> Result<PathBuf, String> {
    if raw.is_empty() || raw.contains('\0') {
        return Err("bad path".into());
    }
    let p = if let Some(rest) = raw.strip_prefix("~/") {
        PathBuf::from(std::env::var("HOME").map_err(|_| "no HOME")?).join(rest)
    } else {
        Path::new("/").join(raw.trim_start_matches('/'))
    };
    p.symlink_metadata().map_err(|_| format!("no such file: {}", p.display()))?;
    Ok(p)
}

/// `url` checked against the known `(name, site)`s: a page on one of their origins (https,
/// or this Mac's own `http://127.0.0.1:…` server); its site's name.
pub fn view_site<'a>(url: &Url, sites: &[(&'a str, Url)]) -> Result<&'a str, String> {
    let local = url.scheme() == "http" && url.host_str() == Some("127.0.0.1");
    if url.scheme() != "https" && !local {
        return Err(format!("url scheme {:?} isn't https", url.scheme()));
    }
    sites.iter().find(|(_, s)| s.origin() == url.origin()).map(|(n, _)| *n).ok_or_else(|| format!("url origin {} isn't a known site", url.origin().ascii_serialization()))
}

/// Opened by their default app only to be displayed: documents, images, audio, video.
/// Not here, on purpose: macro-enabled Office (`docm`, `xlsm`, …), HTML / SVG (a
/// browser runs their scripts), scripts, apps, installers, disk images, archives.
const SAFE_EXTS: &[&str] = &[
    "pdf", "txt", "md", "rtf", "csv", "tsv", "json", "log", "jpg", "jpeg", "png", "gif", "heic", "heif", "webp", "tif", "tiff", "bmp", "dng", "raw", "cr2", "cr3", "nef", "arw",
    "mp4", "m4v", "mov", "webm", "mkv", "avi", "mp3", "m4a", "aac", "wav", "flac", "aif", "aiff", "ogg", "opus", "docx", "xlsx", "pptx", "pages", "numbers", "key", "epub",
];

#[derive(Debug, PartialEq)]
pub enum Opening {
    /// Open in its default app, no question.
    Direct,
    /// A plain folder: open it in Finder.
    Folder,
    /// Ask first; the reason names what kind of thing it is.
    Confirm(String),
}

/// How `open-file` treats `p`, judged on its resolved target.
pub fn classify(p: &Path) -> Result<Opening, String> {
    let real = p.canonicalize().map_err(|e| format!("{}: {e}", p.display()))?;
    let ext = real.extension().and_then(|e| e.to_str()).map(str::to_ascii_lowercase);
    if real.is_dir() {
        return Ok(match ext {
            None => Opening::Folder,
            Some(e) => Opening::Confirm(format!("“{}” is a package (.{e}): opening it can run an app or installer.", name(&real))),
        });
    }
    Ok(match ext {
        Some(e) if SAFE_EXTS.contains(&e.as_str()) => Opening::Direct,
        Some(e) => Opening::Confirm(format!("“{}” is a .{e} file: opening it may run code.", name(&real))),
        None => Opening::Confirm(format!("“{}” has no file type: opening it may run code.", name(&real))),
    })
}

fn name(p: &Path) -> String {
    p.file_name().map_or_else(|| p.display().to_string(), |n| n.to_string_lossy().into_owned())
}

fn open_cmd(args: &[&str], p: &Path) -> Result<(), String> {
    let st = Command::new("/usr/bin/open").args(args).arg(p).status().map_err(|e| e.to_string())?;
    if st.success() { Ok(()) } else { Err(format!("open {} failed ({st})", p.display())) }
}

pub fn reveal(p: &Path) -> Result<(), String> {
    open_cmd(&["-R"], p)
}

/// The native dialog for `Opening::Confirm`: `Some(true)` = open, `Some(false)` = show in Finder, `None` = cancel.
/// The message is passed as an argument, never spliced into the script.
fn confirm(reason: &str) -> Option<bool> {
    let script = [
        "on run argv",
        "display dialog (item 1 of argv) & return & return & \"Open it only if you expected this.\" with title \"disky\" buttons {\"Cancel\", \"Open\", \"Show in Finder\"} default button \"Show in Finder\" cancel button \"Cancel\" with icon caution",
        "return button returned of result",
        "end run",
    ];
    let mut cmd = Command::new("/usr/bin/osascript");
    for l in script {
        cmd.args(["-e", l]);
    }
    let out = cmd.arg(reason).output().ok()?;
    match String::from_utf8_lossy(&out.stdout).trim() {
        "Open" => Some(true),
        "Show in Finder" => Some(false),
        _ => None,
    }
}

/// Carry out `open-file` per `classify`.
pub fn open_file(p: &Path) -> Result<String, String> {
    match classify(p)? {
        Opening::Direct | Opening::Folder => open_cmd(&[], p).map(|_| format!("opened {}", p.display())),
        Opening::Confirm(reason) => match confirm(&reason) {
            Some(true) => open_cmd(&[], p).map(|_| format!("opened {} (confirmed)", p.display())),
            Some(false) => reveal(p).map(|_| format!("revealed {} instead of opening", p.display())),
            None => Ok(format!("didn't open {} (cancelled)", p.display())),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    fn u(s: &str) -> Url {
        Url::parse(s).unwrap()
    }

    #[test]
    fn parses_links() {
        assert_eq!(parse(&u("disky://view?url=https%3A%2F%2Fdisk.rbw.sh%2F~%2FDownloads%3Fopen%3Dx.jpg")), Ok(Some(Link::View(u("https://disk.rbw.sh/~/Downloads?open=x.jpg")))));
        assert_eq!(parse(&u("disky://reveal?path=etc%2Fhosts")), Ok(Some(Link::Reveal("/etc/hosts".into()))));
        assert_eq!(parse(&u("disky://open-file?path=%2Fetc%2Fhosts")), Ok(Some(Link::OpenFile("/etc/hosts".into()))));
        assert_eq!(parse(&u("disky://open?link=x")), Ok(None));
        assert_eq!(parse(&u("disky://reveal")), Err("no `path` parameter".into()));
        assert_eq!(parse(&u("disky://reveal?path=no%2Fsuch%2Fthing")), Err("no such file: /no/such/thing".into()));
        assert_eq!(parse(&u("disky://view?url=nope")), Err("bad url: relative URL without a base".into()));
    }

    #[test]
    fn view_only_known_sites() {
        let sites = [("prod", u("https://disk.rbw.sh")), ("dev", u("https://dev.disk.rbw.sh")), ("local", u("http://127.0.0.1:7792"))];
        let v = |s: &str| view_site(&u(s), &sites);
        assert_eq!(v("https://dev.disk.rbw.sh/~/Downloads?open=a.pdf"), Ok("dev"));
        assert_eq!(v("http://127.0.0.1:7792/Users"), Ok("local"));
        assert_eq!(v("https://evil.example/"), Err("url origin https://evil.example isn't a known site".into()));
        assert_eq!(v("http://disk.rbw.sh/"), Err("url scheme \"http\" isn't https".into()));
        assert_eq!(v("http://127.0.0.1:9999/"), Err("url origin http://127.0.0.1:9999 isn't a known site".into()));
        assert_eq!(v("file:///etc/hosts"), Err("url scheme \"file\" isn't https".into()));
    }

    #[test]
    fn classifies_what_may_open_unasked() {
        let base = std::env::temp_dir().join(format!("disky-links-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("Folder")).unwrap();
        std::fs::create_dir_all(base.join("Thing.app/Contents")).unwrap();
        for f in ["a.JPG", "b.pdf", "run.command", "page.html", "README"] {
            std::fs::write(base.join(f), b"x").unwrap();
        }
        symlink(base.join("Thing.app"), base.join("disguised.jpg")).unwrap();
        let c = |f: &str| classify(&base.join(f));
        assert_eq!(
            ["a.JPG", "b.pdf", "Folder"].map(c),
            [Ok(Opening::Direct), Ok(Opening::Direct), Ok(Opening::Folder)],
        );
        assert_eq!(
            ["run.command", "page.html", "README", "Thing.app", "disguised.jpg"].map(c),
            [
                Ok(Opening::Confirm("“run.command” is a .command file: opening it may run code.".into())),
                Ok(Opening::Confirm("“page.html” is a .html file: opening it may run code.".into())),
                Ok(Opening::Confirm("“README” has no file type: opening it may run code.".into())),
                Ok(Opening::Confirm("“Thing.app” is a package (.app): opening it can run an app or installer.".into())),
                Ok(Opening::Confirm("“Thing.app” is a package (.app): opening it can run an app or installer.".into())),
            ],
        );
        let _ = std::fs::remove_dir_all(&base);
    }
}
