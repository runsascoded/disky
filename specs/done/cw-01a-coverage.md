# CW 01a coverage and quiet digest entries

The CW source profile includes `s3://marin-us-east-01a` through the existing CoreWeave endpoint and read credentials. Both site environments, the frontend bucket list, cache warm-up and digest labeling include the same bucket. A scheduled scan reads the profile embedded in the promoted image, so the source addition requires an image promotion, without changing the scheduler or publishing a partial fleet snapshot.

The 01a quota is not verified. Its digest entry reports measured usage without assigning another zone's quota. Unknown or unscanned buckets are never rendered as empty.

Monthly pool-layout OPs and daily replies co-locate near-zero entries on one line. Each entry retains its link, usage or quota clause and pressure mark; no independent quotas are combined. The initial threshold is less than 0.5 TiB, less than 25% of any known quota and no short runway. A material change over the displayed comparison interval keeps an entry separate even when its current usage is tiny. A bucket grows out of the compact line automatically. The single-line reply format retains its original order.

Python posting and TypeScript previews use the same rule. Regression fixtures cover exact lines, meaningful usage, significant deletions, missing buckets and the unchanged single-line format.

Released on 2026-11-10 UTC: production Pages `2e8c3d34`, dev Pages `7b3fb4f5`, scan image `sha256:db8a15f601bb33178f88447984789bff0a081ae56879f47f68fe784f4e039a05` promoted to `latest`. Cloud Build `25339ba3-258d-4b6a-9dd5-e3836a940cbc` passed 261 tests, both TypeScript checks and the production build; image build `c7b6bbab-e489-467e-b7fd-abe388737a98` succeeded. The next scheduled scan at 06:00 UTC includes all nine configured sources. Existing history and review state was not changed.
