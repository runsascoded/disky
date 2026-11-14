//! `vmd`: VM mode's query server (specs/vm-mode.md).
//!
//! ```text
//! vmd -c CONF           serve CONF's tenants (JSON: listen, threads, handlers, watch_s, tenants[{name, pack | packs,
//!                       shards, key_env, hex_runs, prefault}]); each tenant's key from its `key_env`
//! ```

use std::path::PathBuf;

use full_scan::serve::Server;
use full_scan::util::Result;

fn main() -> Result<()> {
    let mut it = std::env::args().skip(1);
    let mut conf = None;
    while let Some(a) = it.next() {
        match a.as_str() {
            "-c" => conf = it.next(),
            _ => return Err(format!("usage: vmd -c CONF ({a}?)").into()),
        }
    }
    let conf = PathBuf::from(conf.ok_or("usage: vmd -c CONF")?);
    Server::new(conf)?.run()
}
