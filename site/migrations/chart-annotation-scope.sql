-- Chart notes scoped, linked and kinded (spec `chart-annotations.md`): `path` (a note shows at that path and below;
-- NULL = the whole store, as before), `url` (a link, e.g. the Slack message it records), `kind` (a short tag the
-- chart's options toggle, e.g. `cleanup ask`).
ALTER TABLE chart_annotations ADD COLUMN path TEXT;
ALTER TABLE chart_annotations ADD COLUMN url TEXT;
ALTER TABLE chart_annotations ADD COLUMN kind TEXT;
