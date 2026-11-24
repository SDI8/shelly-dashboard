-- Existing installations only: stop Telegraf before running this migration,
-- then start it with the updated emdata.conf. Renaming preserves history.
-- Run only when the old column exists and the new column does not.
-- Fresh installations create the corrected column through ILP automatically.
ALTER TABLE shelly_emdata RENAME COLUMN b_total_act_energy TO b_total_active_energy;