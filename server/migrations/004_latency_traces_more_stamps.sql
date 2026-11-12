-- When the relay passed the candidate's last frame of speech before the brain request, in ms
-- since the call went live (tracing.py). The time from it to the request is Vapi's wait to
-- decide the answer is over plus its speech to text, which no other hop holds. Null when the
-- candidate said nothing loud enough since the line before.

alter table latency_traces add column last_candidate_audio_ms double precision;
