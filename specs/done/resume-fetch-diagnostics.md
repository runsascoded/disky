# Failed fetches around browser resumption

The reported 2026-11-09 23:04 UTC activity burst contains ten HTTP-status-zero fetch failures across two tabs, with their request starts within 56 ms. Requests for the same endpoints succeeded in parallel from other tabs. The records show transport failures, not ten independent HTTP errors or JavaScript exceptions. They support a brief connection interruption around tab resumption, but do not establish the network cause; the original logs contain no online/offline hint. Existing query retry behavior remains in effect.

Future full-page logger fetch failures preserve the failure and also record the browser's online hint, document visibility, and time since the most recent recorded transition to visible. These hints do not prove network reachability, and failures continue to count as errors. No schema change or historical reclassification is needed.

Concurrent batches could read an old actor burst and then find it already extended when checking the tab's prior burst. The latter was counted as a new resumption even when its elapsed gap was zero. The prior-burst path now reports a resumption only when its current gap exceeds the burst gap threshold. The regression injects that exact interleaving against D1; old Slack posts and existing activity records are not rewritten.

Cloud Build `d4d3c060-5db1-450a-a9ba-2f885016b307` passed 69 targeted tests, both TypeScript checks and the production build, including the resumption race and exact failed-fetch metadata.

Released to CW production (`23dd3167`) and dev (`28446fd5`). No scan-source, IAM, object-data or history-pointer changes were included.
