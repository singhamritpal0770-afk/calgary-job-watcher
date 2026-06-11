# Calgary job watcher (cloud)

Watches Amazon, Randstad, Sysco, YYC airport and Job Bank for new
warehouse-type jobs around Calgary and sends instant phone pushes via
[ntfy.sh](https://ntfy.sh).

Runs on GitHub Actions: a scheduled workflow fires every ~5–10 minutes and
each run checks 5 times, 60 seconds apart. Job-board sources are checked on
every 5th check; Amazon on every check (its postings vanish within minutes).

ntfy topic names are **repository secrets** (`NTFY_TOPIC_AMAZON`,
`NTFY_TOPIC_AGENCY`, `NTFY_TOPIC_OTHER`) — never commit them to this public
repo, since anyone who knows a topic name can read or spam it.

`state.json` (already-alerted jobs, 24 h memory) is carried between runs via
`actions/cache`.
