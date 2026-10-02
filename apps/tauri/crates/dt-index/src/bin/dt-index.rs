//! `dt-index -d ASOF -P INDEX_DIR -o SNAP_DIR CAPTURE_DIR` — the Rust
//! `dt-cloud path-index -g` over one local capture: the store's two sorts into
//! INDEX_DIR, `meta.json` + `age.json` into SNAP_DIR.

use std::path::PathBuf;
use std::process::ExitCode;

fn usage(msg: &str) -> ExitCode {
    eprintln!("dt-index: {msg}\nusage: dt-index -d|--asof YYYY-MM-DD -P|--index DIR -o|--out DIR CAPTURE_DIR");
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let (mut asof, mut index, mut out, mut capture) = (None, None, None, None);
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "-d" | "--asof" => asof = args.next(),
            "-P" | "--index" => index = args.next().map(PathBuf::from),
            "-o" | "--out" => out = args.next().map(PathBuf::from),
            "-h" | "--help" => return usage("help"),
            _ if capture.is_none() => capture = Some(PathBuf::from(a)),
            _ => return usage("one CAPTURE_DIR only"),
        }
    }
    let (Some(asof), Some(index), Some(out), Some(capture)) = (asof, index, out, capture) else {
        return usage("need -d, -P, -o and CAPTURE_DIR");
    };
    let Some(day) = dt_index::epoch_day(&asof) else { return usage("--asof must be YYYY-MM-DD") };
    let run = || -> std::io::Result<()> {
        let mut r = dt_index::Reducer::new(day);
        r.push_capture(&capture)?;
        let store = r.finish();
        let sorts = store.write_index(&index)?;
        store.write_snapshot(&out, &asof, &sorts)?;
        eprintln!("{}: {} rows ({} + {} row groups), {} objects, {} bytes", index.display(), sorts[0].rows, sorts[0].groups, sorts[1].groups, store.total_objects, store.total_bytes);
        Ok(())
    };
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("dt-index: {e}");
            ExitCode::FAILURE
        }
    }
}
