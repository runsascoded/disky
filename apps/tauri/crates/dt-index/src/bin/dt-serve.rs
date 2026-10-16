//! `dt-serve [-a ADDR] [-H HOME] [-l ROOT_LABEL] [-s STORE] [-t TITLE] [FILES] -w WEB_DIR SCANS_DIR`
//! — the local read API + SPA (`dt_index::http`) on loopback (default
//! `127.0.0.1:7792`). `HOME` (default `$HOME`) is what the SPA shows as `~`.
//!
//! `FILES` turns on `/files/<path>` (spec `laptop-file-tunnel.md`), for a
//! Cloudflare Tunnel to this port: `-f ROOT` (the store root the paths are
//! relative to) with `--access-team TEAM` (`<team>.cloudflareaccess.com`) and
//! `--access-aud AUD` (the Access app's AUD tag), optionally
//! `--access-client-id ID` (only that service token) and `--access-jwks FILE`
//! (fixed keys instead of fetching the team's certs).

use std::path::PathBuf;
use std::process::ExitCode;

fn usage(msg: &str) -> ExitCode {
    eprintln!("dt-serve: {msg}\nusage: dt-serve [-a ADDR] [-H HOME] [-l ROOT_LABEL] [-s STORE] [-t TITLE] [-f ROOT --access-team TEAM --access-aud AUD [--access-client-id ID] [--access-jwks FILE]] -w WEB_DIR SCANS_DIR");
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let (mut addr, mut label, mut store, mut web, mut scans) = ("127.0.0.1:7792".to_string(), "this Mac".to_string(), "laptop".to_string(), None, None);
    let (mut home, mut title) = (std::env::var("HOME").ok(), None);
    let (mut files_root, mut jwks): (Option<PathBuf>, Option<PathBuf>) = (None, None);
    let (mut team, mut aud, mut client_id): (Option<String>, Option<String>, Option<String>) = (None, None, None);
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "-a" | "--addr" => addr = args.next().unwrap_or(addr),
            "-H" | "--home" => home = args.next(),
            "-f" | "--files-root" => files_root = args.next().map(PathBuf::from),
            "--access-team" => team = args.next(),
            "--access-aud" => aud = args.next(),
            "--access-client-id" => client_id = args.next(),
            "--access-jwks" => jwks = args.next().map(PathBuf::from),
            "-t" | "--title" => title = args.next(),
            "-l" | "--root-label" => label = args.next().unwrap_or(label),
            "-s" | "--store" => store = args.next().unwrap_or(store),
            "-w" | "--web" => web = args.next().map(PathBuf::from),
            "-h" | "--help" => return usage("help"),
            _ if scans.is_none() => scans = Some(PathBuf::from(a)),
            _ => return usage("one SCANS_DIR only"),
        }
    }
    let (Some(web), Some(scans)) = (web, scans) else { return usage("need -w WEB_DIR and SCANS_DIR") };
    let files = match (files_root, team, aud) {
        (None, None, None) if client_id.is_none() && jwks.is_none() => None,
        (Some(root), Some(team), Some(aud)) => {
            let access = match jwks {
                None => dt_index::access::Access::new(&team, &aud, client_id),
                Some(f) => match std::fs::read_to_string(&f).map_err(|e| e.to_string()).and_then(|j| dt_index::access::Access::with_jwks(&team, &aud, client_id, &j)) {
                    Ok(a) => a,
                    Err(e) => return usage(&format!("--access-jwks {}: {e}", f.display())),
                },
            };
            match dt_index::files::Files::new(&root, access) {
                Ok(f) => {
                    eprintln!("dt-serve: /files/ under {} (Access JWT for {team})", f.root.display());
                    Some(f)
                }
                Err(e) => return usage(&format!("-f {}: {e}", root.display())),
            }
        }
        _ => return usage("/files needs -f ROOT, --access-team and --access-aud together"),
    };
    eprintln!("dt-serve: http://{addr}/ ({} scans in {})", dt_index::view::Scans::new(scans.clone()).list().len(), scans.display());
    match dt_index::http::serve(dt_index::http::Config { scans, web, store, root_label: label, home: home.map(|h| h.trim_matches('/').to_string()), title, files }, &addr, 4) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("dt-serve: {e}");
            ExitCode::FAILURE
        }
    }
}
