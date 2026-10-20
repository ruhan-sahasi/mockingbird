-- The first schema: interviews, and what hangs off them. Every child table deletes itself with its
-- interview (on delete cascade), which is what makes the 7-day demo retention one DELETE.

create table interviews (
    id            text primary key,
    mode          text not null check (mode in ('demo', 'full')),
    status        text not null check (status in ('created', 'live', 'ended', 'graded', 'abandoned')),
    visitor_hash  text not null,
    ip_hash       text not null,
    cap_s         integer not null,
    questions     jsonb not null,           -- the main question texts, in order
    created_at    timestamptz not null,
    vapi_call_id  text,                     -- a credential while the call is live: never log it
    ended_reason  text,
    duration_s    integer,
    report        jsonb                     -- the whole graded report; scores and quotes are copies
);

create table turns (
    interview_id  text not null references interviews (id) on delete cascade,
    idx           integer not null,
    speaker       text not null check (speaker in ('interviewer', 'candidate')),
    text          text not null,            -- may be empty: silence is stored as a blank turn
    question_idx  integer not null,
    interrupted   boolean not null default false,
    primary key (interview_id, idx)
);

create table scores (
    id            bigint generated always as identity primary key,
    interview_id  text not null references interviews (id) on delete cascade,
    question_idx  integer not null,
    item          text not null,
    score         integer,                  -- null when there was not enough evidence to score
    rationale     text not null,
    status        text not null
    -- No unique (interview_id, question_idx, item): this table is a copy of the report, and the
    -- grader can repeat an item. A rule here would stop a valid report from being saved at all.
);

create table quotes (
    score_id      bigint not null references scores (id) on delete cascade,
    text          text not null,
    start_ms      integer,                  -- phase 3 fills in where the quote sits in the audio
    end_ms        integer,
    verified      boolean not null
);

-- Supabase serves every table in the public schema over its REST API to anyone holding the
-- project's public key. These tables hold transcripts and a call id, so turn row level security
-- on and add no policies: the REST API then sees nothing. The server connects as the table owner
-- (the postgres role), which row level security does not apply to.
alter table interviews enable row level security;
alter table turns enable row level security;
alter table scores enable row level security;
alter table quotes enable row level security;
