# Calgary job watcher (cloud)

Watches Amazon, Sysco, YYC airport and Job Bank for new warehouse-type jobs
around Calgary and sends instant phone pushes via [ntfy.sh](https://ntfy.sh).

Runs on GitHub Actions: a scheduled workflow fires every ~5–10 minutes and
each run checks 5 times, 60 seconds apart. Job-board sources are checked on
every 5th check; Amazon on every check — though Amazon's WAF usually blocks
cloud runner IPs (403), so Amazon coverage mainly comes from the companion
watcher running on a residential connection.

ntfy topic names are **repository secrets** (`NTFY_TOPIC_AMAZON`,
`NTFY_TOPIC_OTHER`) — never commit them to this public repo, since anyone
who knows a topic name can read or spam it.

`state.json` (already-alerted jobs, 72 h memory) is carried between runs via
`actions/cache`. A run that starts with no state records every job it sees
without alerting, so a lost cache never re-spams old postings.
