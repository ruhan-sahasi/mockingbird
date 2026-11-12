-- Two more numbers for each spoken reply (tracing.py), both null when not measured.
--
-- last_candidate_audio_ms: when the relay passed the candidate's last frame of speech before the
-- brain request, in ms since the call went live. The time from it to the request is Vapi's wait
-- to decide the answer is over plus its speech to text, which no other hop holds.
--
-- talk_over_ms: measured by the browser itself, how far into the reply, as the candidate heard
-- it, they started to talk over it. It decides cut_in when the browser sent it.

alter table latency_traces add column last_candidate_audio_ms double precision;
alter table latency_traces add column talk_over_ms double precision;
