//! `disky://open?link=<url>`: the site's sign-in hand-off (`specs/app-link.md`).
//!
//! The signed-in browser mints a 60 s, single-use link and opens it via this
//! scheme; the app loads it in its window, which redeems it into an ordinary
//! session cookie. The link is a credential for whoever loads it first, so the
//! only URL accepted is the redeem endpoint on the configured site's own origin
//! over https (`/auth/app-link`); anything else is refused and logged.

use tauri::Url;

pub const REDEEM_PATH: &str = "/auth/app-link";

/// The URL to load for deep link `deep`, given the site the window serves.
pub fn link_to_load(deep: &Url, site: &Url) -> Result<Url, String> {
    if deep.scheme() != "disky" || deep.host_str() != Some("open") {
        return Err(format!("not a disky://open link: {}", deep.scheme()));
    }
    let raw = deep
        .query_pairs()
        .find(|(k, _)| k == "link")
        .map(|(_, v)| v.into_owned())
        .ok_or("no `link` parameter")?;
    let link = Url::parse(&raw).map_err(|e| format!("bad link: {e}"))?;
    if link.scheme() != "https" {
        return Err(format!("link scheme {:?} isn't https", link.scheme()));
    }
    if link.origin() != site.origin() {
        return Err(format!("link origin {} isn't the site's ({})", link.origin().ascii_serialization(), site.origin().ascii_serialization()));
    }
    if link.path() != REDEEM_PATH {
        return Err(format!("link path {:?} isn't {REDEEM_PATH}", link.path()));
    }
    Ok(link)
}

/// The webview's user agent: WKWebView's default shape plus `disky/<version>`,
/// which the site reads to hide its "Open in disky" button inside the app.
pub fn user_agent() -> String {
    format!(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) disky/{}",
        env!("CARGO_PKG_VERSION")
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(deep: &str) -> Result<String, String> {
        let site = Url::parse("https://disk.rbw.sh").unwrap();
        link_to_load(&Url::parse(deep).unwrap(), &site).map(|u| u.to_string())
    }

    #[test]
    fn accepts_the_redeem_url_on_the_site_origin() {
        assert_eq!(
            check("disky://open?link=https%3A%2F%2Fdisk.rbw.sh%2Fauth%2Fapp-link%3Ftoken%3Dabc%26next%3D%252FUsers"),
            Ok("https://disk.rbw.sh/auth/app-link?token=abc&next=%2FUsers".to_string()),
        );
    }

    #[test]
    fn refuses_everything_else() {
        let errs = [
            "disky://open?link=http%3A%2F%2Fdisk.rbw.sh%2Fauth%2Fapp-link%3Ftoken%3Dabc",
            "disky://open?link=https%3A%2F%2Fevil.example%2Fauth%2Fapp-link%3Ftoken%3Dabc",
            "disky://open?link=https%3A%2F%2Fdisk.rbw.sh.evil.example%2Fauth%2Fapp-link",
            "disky://open?link=https%3A%2F%2Fdisk.rbw.sh%3A8443%2Fauth%2Fapp-link",
            "disky://open?link=https%3A%2F%2Fdisk.rbw.sh%2Fapi%2Fdelete",
            "disky://open?link=javascript%3Aalert(1)",
            "disky://open",
            "disky://other?link=https%3A%2F%2Fdisk.rbw.sh%2Fauth%2Fapp-link",
        ]
        .map(|d| check(d).map_err(|e| e.split(':').next().unwrap().to_string()));
        assert_eq!(
            errs,
            [
                Err("link scheme \"http\" isn't https".into()),
                Err("link origin https".into()),
                Err("link origin https".into()),
                Err("link origin https".into()),
                Err("link path \"/api/delete\" isn't /auth/app-link".into()),
                Err("link scheme \"javascript\" isn't https".into()),
                Err("no `link` parameter".into()),
                Err("not a disky".into()),
            ]
        );
    }

    #[test]
    fn user_agent_carries_the_disky_token() {
        assert!(user_agent().ends_with(&format!(" disky/{}", env!("CARGO_PKG_VERSION"))));
    }
}
