-- One row per spoken interviewer reply: when each hop of it happened, in ms since the call went
-- live (tracing.py). A hop that was not measured is null, and never counts as 0 ms. The rows go
-- with their interview (on delete cascade), so the 7-day demo purge takes them too.

create table latency_traces (
    interview_id                text not null references interviews (id) on delete cascade,
    turn_idx                    integer not null,   -- the interviewer turn this reply became
    user_stopped_ms             double precision,
    request_ms                  double precision,
    first_token_ms              double precision,
    first_chunk_ms              double precision,
    assistant_started_ms        double precision,
    first_audio_ms              double precision,
    browser_stop_to_audible_ms  double precision,   -- measured by the browser itself
    interrupt_to_silence_ms     double precision,   -- measured by the browser itself
    cut_in                      boolean not null default false,
    created_at                  timestamptz not null default now(),
    primary key (interview_id, turn_idx)
);

-- The same reason as in 001_init.sql: keep Supabase's REST API out. No policies.
alter table latency_traces enable row level security;
