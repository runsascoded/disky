//! Cloudflare Access JWT checks for `/files` (spec `laptop-file-tunnel.md`): a
//! request through the tunnel carries `Cf-Access-Jwt-Assertion`, an RS256 JWT
//! signed by the team's Access keys (`https://<team>/cdn-cgi/access/certs`).
//! The loopback listener trusts nothing else that reaches it.
//!
//! A service-token JWT's claims: `aud` (the Access app's AUD tag, a string or
//! a list), `iss` (`https://<team>`), `exp`, `iat`, `nbf`, `type: "app"`,
//! `common_name` (the service token's client id), `sub: ""`. Checked: `alg`
//! RS256 with a known `kid`, the signature, `iss`, `aud`, `exp` / `nbf` (60 s
//! leeway), and `common_name` when a client id is pinned.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use ring::signature::{RsaPublicKeyComponents, RSA_PKCS1_2048_8192_SHA256};
use serde_json::Value;

const LEEWAY: u64 = 60;
/// An unknown `kid` refetches the certs (Access rotates its keys), at most this often.
const REFETCH: Duration = Duration::from_secs(60);

struct Key {
    n: Vec<u8>,
    e: Vec<u8>,
}

struct Keys {
    by_kid: HashMap<String, Key>,
    fetched: Option<Instant>,
}

pub struct Access {
    issuer: String,
    aud: String,
    /// The service token's client id; `None` accepts any identity the app's policy let through.
    client_id: Option<String>,
    /// `None`: a fixed key set (`with_jwks`), never refetched.
    certs_url: Option<String>,
    keys: Mutex<Keys>,
}

/// The claims a verified token carried.
#[derive(Debug, PartialEq)]
pub struct Claims {
    pub common_name: Option<String>,
    pub email: Option<String>,
    pub exp: u64,
}

fn parse_jwks(json: &str) -> Result<HashMap<String, Key>, String> {
    let v: Value = serde_json::from_str(json).map_err(|e| format!("jwks: {e}"))?;
    let keys = v["keys"].as_array().ok_or("jwks: no keys")?;
    let mut out = HashMap::new();
    for k in keys {
        if k["kty"] != "RSA" {
            continue;
        }
        let (Some(kid), Some(n), Some(e)) = (k["kid"].as_str(), k["n"].as_str(), k["e"].as_str()) else { continue };
        let b = |s: &str| URL_SAFE_NO_PAD.decode(s.trim_end_matches('=')).map_err(|e| format!("jwks {kid}: {e}"));
        out.insert(kid.to_string(), Key { n: b(n)?, e: b(e)? });
    }
    Ok(out)
}

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// `rbw.cloudflareaccess.com` or `https://rbw.cloudflareaccess.com/` → `https://rbw.cloudflareaccess.com`.
fn issuer(team: &str) -> String {
    let t = team.trim().trim_end_matches('/');
    let host = t.strip_prefix("https://").unwrap_or(t);
    format!("https://{host}")
}

impl Access {
    /// Keys fetched from the team's certs endpoint, on first use and on an unknown `kid`.
    pub fn new(team: &str, aud: &str, client_id: Option<String>) -> Self {
        let issuer = issuer(team);
        let certs_url = Some(format!("{issuer}/cdn-cgi/access/certs"));
        Access { issuer, aud: aud.into(), client_id, certs_url, keys: Mutex::new(Keys { by_kid: HashMap::new(), fetched: None }) }
    }

    /// A fixed key set (a JWKS document): tests, or `--access-jwks FILE` offline.
    pub fn with_jwks(team: &str, aud: &str, client_id: Option<String>, jwks: &str) -> Result<Self, String> {
        let by_kid = parse_jwks(jwks)?;
        Ok(Access { issuer: issuer(team), aud: aud.into(), client_id, certs_url: None, keys: Mutex::new(Keys { by_kid, fetched: Some(Instant::now()) }) })
    }

    fn key(&self, kid: &str) -> Result<(Vec<u8>, Vec<u8>), String> {
        let mut keys = self.keys.lock().unwrap();
        let stale = keys.fetched.is_none_or(|t| t.elapsed() >= REFETCH);
        if !keys.by_kid.contains_key(kid) && stale {
            if let Some(url) = &self.certs_url {
                keys.fetched = Some(Instant::now());
                let body = ureq::get(url).timeout(Duration::from_secs(10)).call().map_err(|e| format!("certs: {e}"))?.into_string().map_err(|e| format!("certs: {e}"))?;
                keys.by_kid = parse_jwks(&body)?;
            }
        }
        keys.by_kid.get(kid).map(|k| (k.n.clone(), k.e.clone())).ok_or_else(|| format!("unknown kid {kid}"))
    }

    pub fn verify(&self, token: &str) -> Result<Claims, String> {
        self.verify_at(token, now())
    }

    pub fn verify_at(&self, token: &str, now: u64) -> Result<Claims, String> {
        let mut parts = token.trim().split('.');
        let (Some(h), Some(p), Some(s), None) = (parts.next(), parts.next(), parts.next(), parts.next()) else { return Err("not a JWT".into()) };
        let json = |seg: &str, what: &str| -> Result<Value, String> {
            let bytes = URL_SAFE_NO_PAD.decode(seg).map_err(|e| format!("{what}: {e}"))?;
            serde_json::from_slice(&bytes).map_err(|e| format!("{what}: {e}"))
        };
        let header = json(h, "header")?;
        if header["alg"] != "RS256" {
            return Err(format!("alg {} (want RS256)", header["alg"]));
        }
        let kid = header["kid"].as_str().ok_or("no kid")?;
        let (n, e) = self.key(kid)?;
        let sig = URL_SAFE_NO_PAD.decode(s).map_err(|e| format!("signature: {e}"))?;
        RsaPublicKeyComponents { n: &n, e: &e }.verify(&RSA_PKCS1_2048_8192_SHA256, format!("{h}.{p}").as_bytes(), &sig).map_err(|_| "bad signature")?;
        let c = json(p, "payload")?;
        if c["iss"].as_str() != Some(self.issuer.as_str()) {
            return Err(format!("iss {} (want {})", c["iss"], self.issuer));
        }
        let aud_ok = match &c["aud"] {
            Value::String(a) => *a == self.aud,
            Value::Array(a) => a.iter().any(|x| x.as_str() == Some(self.aud.as_str())),
            _ => false,
        };
        if !aud_ok {
            return Err("aud mismatch".into());
        }
        let exp = c["exp"].as_u64().ok_or("no exp")?;
        if exp + LEEWAY < now {
            return Err("expired".into());
        }
        if c["nbf"].as_u64().is_some_and(|nbf| nbf > now + LEEWAY) {
            return Err("not yet valid".into());
        }
        let common_name = c["common_name"].as_str().map(String::from);
        if let Some(id) = &self.client_id {
            if common_name.as_deref() != Some(id.as_str()) {
                return Err("not the pinned service token".into());
            }
        }
        Ok(Claims { common_name, email: c["email"].as_str().map(String::from), exp })
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use ring::rand::SystemRandom;
    use ring::signature::{RsaKeyPair, RSA_PKCS1_SHA256};

    /// A fixed 2048-bit test key (PKCS#8 DER, base64): `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -outform DER`.
    const TEST_KEY: &str = include_str!("../tests/access-test-key.b64");
    pub const TEAM: &str = "test.cloudflareaccess.com";
    pub const AUD: &str = "aud-tag-0123";
    pub const CLIENT: &str = "client-id.access";

    fn pair() -> RsaKeyPair {
        let der = base64::engine::general_purpose::STANDARD.decode(TEST_KEY.split_whitespace().collect::<String>()).unwrap();
        RsaKeyPair::from_pkcs8(&der).unwrap()
    }

    pub fn jwks(kid: &str) -> String {
        let p = pair();
        let pk = p.public();
        let c: RsaPublicKeyComponents<Vec<u8>> = pk.into();
        let b = |x: &[u8]| URL_SAFE_NO_PAD.encode(x.iter().skip_while(|b| **b == 0).copied().collect::<Vec<_>>());
        serde_json::json!({"keys": [{"kty": "RSA", "alg": "RS256", "kid": kid, "n": b(&c.n), "e": b(&c.e)}]}).to_string()
    }

    pub fn token(kid: &str, claims: &Value) -> String {
        let h = URL_SAFE_NO_PAD.encode(serde_json::json!({"alg": "RS256", "kid": kid, "typ": "JWT"}).to_string());
        let p = URL_SAFE_NO_PAD.encode(claims.to_string());
        let msg = format!("{h}.{p}");
        let kp = pair();
        let mut sig = vec![0; kp.public().modulus_len()];
        kp.sign(&RSA_PKCS1_SHA256, &SystemRandom::new(), msg.as_bytes(), &mut sig).unwrap();
        format!("{msg}.{}", URL_SAFE_NO_PAD.encode(sig))
    }

    pub fn claims(now: u64) -> Value {
        serde_json::json!({"aud": [AUD], "iss": format!("https://{TEAM}"), "exp": now + 3600, "iat": now, "nbf": now, "type": "app", "common_name": CLIENT, "sub": ""})
    }

    pub fn access(client: Option<&str>) -> Access {
        Access::with_jwks(TEAM, AUD, client.map(String::from), &jwks("k1")).unwrap()
    }

    #[test]
    fn verifies_service_token() {
        let now = 1_800_000_000;
        assert_eq!(access(Some(CLIENT)).verify_at(&token("k1", &claims(now)), now), Ok(Claims { common_name: Some(CLIENT.into()), email: None, exp: now + 3600 }));
        let mut c = claims(now);
        c["aud"] = AUD.into();
        assert_eq!(access(None).verify_at(&token("k1", &c), now).map(|c| c.exp), Ok(now + 3600));
    }

    #[test]
    fn refuses_bad_tokens() {
        let now = 1_800_000_000;
        let a = access(Some(CLIENT));
        let with = |k: &str, v: Value| {
            let mut c = claims(now);
            c[k] = v;
            a.verify_at(&token("k1", &c), now).unwrap_err()
        };
        assert_eq!(with("aud", "other".into()), "aud mismatch");
        assert_eq!(with("iss", "https://evil.cloudflareaccess.com".into()), "iss \"https://evil.cloudflareaccess.com\" (want https://test.cloudflareaccess.com)");
        assert_eq!(with("exp", (now - 61).into()), "expired");
        assert_eq!(with("nbf", (now + 61).into()), "not yet valid");
        assert_eq!(with("common_name", "someone-else".into()), "not the pinned service token");
        assert_eq!(a.verify_at(&token("k2", &claims(now)), now).unwrap_err(), "unknown kid k2");
        assert_eq!(a.verify_at("", now).unwrap_err(), "not a JWT");
        assert_eq!(a.verify_at("a.b", now).unwrap_err(), "not a JWT");
        // A re-signed payload: the signature no longer matches.
        let good = token("k1", &claims(now));
        let mut forged = claims(now);
        forged["exp"] = (now + 99999).into();
        let parts: Vec<_> = good.split('.').collect();
        let tampered = format!("{}.{}.{}", parts[0], URL_SAFE_NO_PAD.encode(forged.to_string()), parts[2]);
        assert_eq!(a.verify_at(&tampered, now).unwrap_err(), "bad signature");
        // `alg: none` with an empty signature.
        let none = format!("{}.{}.", URL_SAFE_NO_PAD.encode(r#"{"alg":"none","kid":"k1"}"#), parts[1]);
        assert_eq!(a.verify_at(&none, now).unwrap_err(), "alg \"none\" (want RS256)");
    }

    #[test]
    fn normalizes_issuer() {
        assert_eq!(issuer("rbw.cloudflareaccess.com"), "https://rbw.cloudflareaccess.com");
        assert_eq!(issuer("https://rbw.cloudflareaccess.com/"), "https://rbw.cloudflareaccess.com");
    }
}
