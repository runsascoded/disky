# Show VM allocation separately from indexed files

The existing scheduled captures already record APFS container capacity, used/free space and each volume's allocation. The one-filesystem path walk excludes the separately mounted VM volume, so a reboot's swap reclamation does not appear in the file-size chart or treemap diff. No schedule change or extra filesystem walk is needed.

`capture-meta` now preserves the sum of allocations for volumes with the APFS `VM` role as `disk_space.vm_used`. Zero is a real reading; older captures without volume measurements omit it. Other volume names and metadata are not published by this change.

The capacity bar displays “VM (swap volume)”. A separate “Whole-disk space at scan” table above the file diff compares physical free space, disk used and VM allocation. The comparison requires matching devices and capacities, and VM requires a reading at both ends. These are whole-container measurements regardless of the current folder or filter; VM allocation includes swap and filesystem overhead, rather than claiming to be a live `sysctl` swap counter. The existing file-size history chart retains its path-based meaning.

For the October 12 reboot at 06:26:59 EDT, scans at 06:08 and 06:39 bracket the restart. VM allocation fell from 34,368,950,272 bytes (32.0 GiB) to 24,576 bytes (24 KiB). Physical free space rose from 21,033,992,192 to 62,103,363,584 bytes, an increase of 38.25 GiB. Both snapshots received the VM annotation from their retained raw manifests; earlier history is not backfilled by this change.

Validation: 22 capture/metadata/ingest tests, 736 site tests (one skipped), SPA/Functions typechecks and production build passed. RACx Chrome verified the capacity label and complete physical comparison on dev and production. Deployment details and the live image verification are recorded in `log.md`.

[Production reboot comparison]

[Production reboot comparison]: https://disk.rbw.sh/?d=261012-1039#diff
