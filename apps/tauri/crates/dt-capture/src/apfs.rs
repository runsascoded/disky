//! The APFS container a path lives on — the Rust twin of `disk_tree.apfs`
//! (`disk-tree volumes`), recorded as `_SUCCESS.json.container`. Same JSON
//! shape: `{device, capacity, free, used, volumes: [{device, name, roles,
//! used, mount, snapshots: [{name, xid, purgeable, limits_shrink}]}]}`, volumes
//! biggest first.

use std::collections::BTreeMap;
use std::process::Command;

use serde::Serialize;

#[derive(Serialize, Debug, PartialEq)]
pub struct Snapshot {
    pub name: String,
    pub xid: u64,
    pub purgeable: bool,
    pub limits_shrink: bool,
}

#[derive(Serialize, Debug, PartialEq)]
pub struct Volume {
    pub device: String,
    pub name: String,
    pub roles: Vec<String>,
    pub used: u64,
    pub mount: Option<String>,
    pub snapshots: Vec<Snapshot>,
}

#[derive(Serialize, Debug, PartialEq)]
pub struct Container {
    pub device: String,
    pub capacity: u64,
    pub free: u64,
    pub used: u64,
    pub volumes: Vec<Volume>,
}

fn run(cmd: &str, args: &[&str]) -> Result<Vec<u8>, String> {
    let o = Command::new(cmd).args(args).output().map_err(|e| format!("{cmd}: {e}"))?;
    if !o.status.success() {
        return Err(format!("{cmd} {}: exit {}", args.join(" "), o.status));
    }
    Ok(o.stdout)
}

fn dict(bytes: &[u8]) -> Result<plist::Dictionary, String> {
    plist::from_bytes::<plist::Value>(bytes)
        .map_err(|e| e.to_string())?
        .into_dictionary()
        .ok_or_else(|| "plist isn't a dict".into())
}

/// `/dev/<dev>` → mount point, from `mount` output.
pub fn device_mounts(mount_output: &str) -> BTreeMap<String, String> {
    mount_output
        .lines()
        .filter_map(|l| {
            let rest = l.strip_prefix("/dev/")?;
            let (dev, rest) = rest.split_once(" on ")?;
            let end = rest.rfind(" (")?;
            let path = &rest[..end];
            let path = path.split(" type ").next().unwrap_or(path);
            Some((dev.to_string(), path.to_string()))
        })
        .collect()
}

/// Pure assembly from the parsed `diskutil` plists + mounts (for tests).
pub fn build(
    info: &plist::Dictionary,
    apfs_list: &plist::Dictionary,
    snapshots: &BTreeMap<String, plist::Dictionary>,
    mounts: &BTreeMap<String, String>,
) -> Result<Container, String> {
    let s = |d: &plist::Dictionary, k: &str| d.get(k).and_then(|v| v.as_string()).map(str::to_string);
    let u = |d: &plist::Dictionary, k: &str| d.get(k).and_then(|v| v.as_unsigned_integer()).unwrap_or(0);
    let b = |d: &plist::Dictionary, k: &str| d.get(k).and_then(|v| v.as_boolean()).unwrap_or(false);
    let reference = s(info, "APFSContainerReference").ok_or("not an APFS volume")?;
    let c = apfs_list
        .get("Containers")
        .and_then(|v| v.as_array())
        .into_iter()
        .flatten()
        .filter_map(|c| c.as_dictionary())
        .find(|c| s(c, "ContainerReference").as_deref() == Some(&reference))
        .ok_or_else(|| format!("container {reference} not in `diskutil apfs list`"))?;
    let mut volumes: Vec<Volume> = c
        .get("Volumes")
        .and_then(|v| v.as_array())
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_dictionary())
        .map(|v| {
            let dev = s(v, "DeviceIdentifier").unwrap_or_default();
            // The volume itself or its snapshot (`disk3s1s1` is the sealed System
            // volume at `/`); the shortest mount path is the one people know.
            let mount = mounts
                .iter()
                .filter(|(d, _)| **d == dev || d.starts_with(&format!("{dev}s")))
                .map(|(_, p)| p.clone())
                .min_by_key(|p| p.len());
            let snaps = snapshots
                .get(&dev)
                .and_then(|d| d.get("Snapshots"))
                .and_then(|v| v.as_array())
                .into_iter()
                .flatten()
                .filter_map(|x| x.as_dictionary())
                .map(|x| Snapshot {
                    name: s(x, "SnapshotName").unwrap_or_default(),
                    xid: u(x, "SnapshotXID"),
                    purgeable: b(x, "Purgeable"),
                    limits_shrink: b(x, "LimitingContainerShrink"),
                })
                .collect();
            let roles = v
                .get("Roles")
                .and_then(|r| r.as_array())
                .into_iter()
                .flatten()
                .filter_map(|r| r.as_string().map(str::to_string))
                .collect();
            Volume { name: s(v, "Name").unwrap_or_default(), roles, used: u(v, "CapacityInUse"), mount, snapshots: snaps, device: dev }
        })
        .collect();
    volumes.sort_by(|a, b| b.used.cmp(&a.used));
    let (capacity, free) = (u(c, "CapacityCeiling"), u(c, "CapacityFree"));
    Ok(Container { device: reference, capacity, free, used: capacity.saturating_sub(free), volumes })
}

/// The live container holding `path` (via `df -P` → `diskutil`).
pub fn container_for(path: &str) -> Result<Container, String> {
    let df = String::from_utf8_lossy(&run("df", &["-P", path])?).into_owned();
    let dev = df.lines().last().and_then(|l| l.split_whitespace().next()).ok_or("no `df` output")?.to_string();
    if !dev.starts_with("/dev/") {
        return Err(format!("{path} is on {dev}, not a local disk"));
    }
    let info = dict(&run("diskutil", &["info", "-plist", &dev])?)?;
    let list = dict(&run("diskutil", &["apfs", "list", "-plist"])?)?;
    let reference = info.get("APFSContainerReference").and_then(|v| v.as_string()).ok_or("not an APFS volume")?.to_string();
    let mut snapshots = BTreeMap::new();
    let devs: Vec<String> = list
        .get("Containers")
        .and_then(|v| v.as_array())
        .into_iter()
        .flatten()
        .filter_map(|c| c.as_dictionary())
        .filter(|c| c.get("ContainerReference").and_then(|v| v.as_string()) == Some(&reference))
        .flat_map(|c| c.get("Volumes").and_then(|v| v.as_array()).cloned().unwrap_or_default())
        .filter_map(|v| v.as_dictionary()?.get("DeviceIdentifier")?.as_string().map(str::to_string))
        .collect();
    for d in devs {
        // A locked or unmountable volume has no listing; not an error.
        if let Ok(out) = run("diskutil", &["apfs", "listSnapshots", "-plist", &d]) {
            if let Ok(x) = dict(&out) {
                snapshots.insert(d, x);
            }
        }
    }
    let mounts = device_mounts(&String::from_utf8_lossy(&run("mount", &[])?));
    build(&info, &list, &snapshots, &mounts)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_mounts() {
        let m = device_mounts(
            "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\ndevfs on /dev (devfs, local, nobrowse)\n/dev/disk5s1 on /Volumes/crucial x6 (apfs, local)\n/dev/disk3s1 on /System/Volumes/Update/mnt1 (apfs, sealed)\n",
        );
        let pairs: Vec<(&str, &str)> = m.iter().map(|(a, b)| (a.as_str(), b.as_str())).collect();
        assert_eq!(pairs, [("disk3s1", "/System/Volumes/Update/mnt1"), ("disk3s1s1", "/"), ("disk5s1", "/Volumes/crucial x6")]);
    }
}
