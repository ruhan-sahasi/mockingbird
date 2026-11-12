-- What became of an interview's call recording: {"status": "uploaded" | "upload_failed", ...}.
-- The audio itself is in the private `recordings` storage bucket, not in this table.
alter table interviews add column recording jsonb;
