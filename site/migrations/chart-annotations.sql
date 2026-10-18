-- Hand-written notes pinned to dates on the "Size over time" chart (spec `chart-annotations.md`): one row per note,
-- per store (`primary`, or a `STORES_JSON` key), at a scan id or date (`YYYY-MM-DD[THHMM]`). Admin-authored;
-- every write also lands in `admin_edits` (`tbl = 'chart_annotations'`).
CREATE TABLE chart_annotations (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  store   TEXT NOT NULL,
  at      TEXT NOT NULL,
  text    TEXT NOT NULL,
  author  TEXT NOT NULL,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL
);
CREATE INDEX chart_annotations_store ON chart_annotations (store, at);
