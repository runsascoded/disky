-- Coarse index tiers (specs/view-serving.md §1): a tier's absolute byte floor
-- F, recorded beside its footer so the reader can plan "coarse vs floor-free"
-- per query without touching the parquet. NULL for the floor-free variants.
ALTER TABLE index_schema ADD COLUMN floor_bytes INTEGER;
