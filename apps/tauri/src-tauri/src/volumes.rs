//! The boot container's other APFS volumes: space a walk of `/` can't see
//! (`src/disk_tree/apfs.py`, `disk-tree volumes`). The walk covers the
//! sealed System volume and the Data volume (through its firmlinks); Preboot,
//! VM (swap), Recovery, Update and any volume of your own are separate
//! filesystems it stays off. A whole-machine scan gets one row per such
//! volume, `(other volumes)/<name>`, sized by its `CapacityInUse`.
//! Snapshots have no size short of a diff, so they aren't rows.

use std::process::Command;

/// The tree a whole-machine scan files the other volumes under.
pub const DIR: &str = "(other volumes)";

/// `(name, bytes in use)` for each volume in container `container` (a
/// `ContainerReference`, `disk3`) of `diskutil apfs list -plist`, but the
/// ones a walk of `/` covers (roles `System`, `Data`), heaviest first.
pub fn others(apfs_list: &plist::Value, container: &str) -> Vec<(String, u64)> {
    let containers = apfs_list.as_dictionary().and_then(|d| d.get("Containers")).and_then(|c| c.as_array());
    let Some(c) = containers.into_iter().flatten().find(|c| c.as_dictionary().and_then(|d| d.get("ContainerReference")).and_then(|r| r.as_string()) == Some(container)) else {
        return vec![];
    };
    let vols = c.as_dictionary().and_then(|d| d.get("Volumes")).and_then(|v| v.as_array());
    let mut out: Vec<(String, u64)> = vols
        .into_iter()
        .flatten()
        .filter_map(|v| {
            let d = v.as_dictionary()?;
            let roles: Vec<&str> = d.get("Roles").and_then(|r| r.as_array()).into_iter().flatten().filter_map(|r| r.as_string()).collect();
            if roles.iter().any(|r| *r == "System" || *r == "Data") {
                return None;
            }
            let used = d.get("CapacityInUse").and_then(|u| u.as_unsigned_integer())?;
            let name = d.get("Name").and_then(|n| n.as_string()).unwrap_or("?").replace('/', "∕");
            (used > 0).then_some((name, used))
        })
        .collect();
    out.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    out
}

/// The boot container's other volumes, live (`diskutil`); empty when it
/// can't tell.
pub fn boot_others() -> Vec<(String, u64)> {
    let plist_of = |args: &[&str]| -> Option<plist::Value> {
        let o = Command::new("/usr/sbin/diskutil").args(args).output().ok().filter(|o| o.status.success())?;
        plist::from_bytes(&o.stdout).ok()
    };
    let Some(info) = plist_of(&["info", "-plist", "/"]) else { return vec![] };
    let Some(container) = info.as_dictionary().and_then(|d| d.get("APFSContainerReference")).and_then(|r| r.as_string()).map(String::from) else { return vec![] };
    plist_of(&["apfs", "list", "-plist"]).map_or(vec![], |l| others(&l, &container))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn picks_unwalked_volumes() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>Containers</key><array>
  <dict><key>ContainerReference</key><string>disk1</string><key>Volumes</key><array>
    <dict><key>Name</key><string>iSCPreboot</string><key>Roles</key><array><string>Preboot</string></array><key>CapacityInUse</key><integer>7000</integer></dict>
  </array></dict>
  <dict><key>ContainerReference</key><string>disk3</string><key>Volumes</key><array>
    <dict><key>Name</key><string>Macintosh HD</string><key>Roles</key><array><string>System</string></array><key>CapacityInUse</key><integer>12000</integer></dict>
    <dict><key>Name</key><string>Data</string><key>Roles</key><array><string>Data</string></array><key>CapacityInUse</key><integer>400000</integer></dict>
    <dict><key>Name</key><string>Preboot</string><key>Roles</key><array><string>Preboot</string></array><key>CapacityInUse</key><integer>20000</integer></dict>
    <dict><key>Name</key><string>VM</string><key>Roles</key><array><string>VM</string></array><key>CapacityInUse</key><integer>3000</integer></dict>
    <dict><key>Name</key><string>Update</string><key>Roles</key><array><string>Update</string></array><key>CapacityInUse</key><integer>0</integer></dict>
    <dict><key>Name</key><string>a/b</string><key>Roles</key><array/><key>CapacityInUse</key><integer>3000</integer></dict>
  </array></dict>
</array></dict></plist>"#;
        let v: plist::Value = plist::from_bytes(xml.as_bytes()).unwrap();
        assert_eq!(others(&v, "disk3"), [("Preboot".to_string(), 20000), ("VM".to_string(), 3000), ("a∕b".to_string(), 3000)]);
        assert_eq!(others(&v, "disk9"), []);
    }
}
