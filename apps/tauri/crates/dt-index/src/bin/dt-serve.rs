//! `dt-serve [-a ADDR] [-H HOME] [-l ROOT_LABEL] [-s STORE] [-t TITLE] -w WEB_DIR SCANS_DIR`
//! — the local read API + SPA (`dt_index::http`) on loopback (default
//! `127.0.0.1:7792`). `HOME` (default `$HOME`) is what the SPA shows as `~`.

use std::path::PathBuf;
use std::process::ExitCode;

fn usage(msg: &str) -> ExitCode {
    eprintln!("dt-serve: {msg}\nusage: dt-serve [-a ADDR] [-H HOME] [-l ROOT_LABEL] [-s STORE] [-t TITLE] -w WEB_DIR SCANS_DIR");
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let (mut addr, mut label, mut store, mut web, mut scans) = ("127.0.0.1:7792".to_string(), "this Mac".to_string(), "laptop".to_string(), None, None);
    let (mut home, mut title) = (std::env::var("HOME").ok(), None);
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "-a" | "--addr" => addr = args.next().unwrap_or(addr),
            "-H" | "--home" => home = args.next(),
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
    eprintln!("dt-serve: http://{addr}/ ({} scans in {})", dt_index::view::Scans::new(scans.clone()).list().len(), scans.display());
    match dt_index::http::serve(dt_index::http::Config { scans, web, store, root_label: label, home: home.map(|h| h.trim_matches('/').to_string()), title }, &addr, 4) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("dt-serve: {e}");
            ExitCode::FAILURE
        }
    }
}
