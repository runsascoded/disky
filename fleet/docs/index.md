# disky

disky indexes object stores (S3, R2, GCS, Hugging Face) and serves each one's usage as a treemap, its changes as diffs, and its history as series. Each deployment is one store's site; this site is their front door.

- [The project](../../README.md): the engine, the cloud overlay, and how a deployment is built.
- [Fleet](/fleet): each deployment's newest scans, their duration and cost, and its `/meta` store's size. Staff sign-in.

A deployment branch replaces this page with its own index (`FLEET_DOCS_INDEX` at build): its deployments, their links and their live preview cards.
