# Calgary job watcher (cloud)

Watches Sysco, YYC airport and Job Bank for new warehouse-type jobs around
Calgary and sends instant phone pushes via [ntfy.sh](https://ntfy.sh).

Runs on GitHub Actions: a scheduled workflow fires every ~5–15 minutes and
checks every source once per run. Amazon (hiring.amazon.ca) is not watched
from here — its WAF 403-blocks cloud runner IPs, so a companion watcher on
a residential connection covers Amazon.

The ntfy topic name is a **repository secret** (`NTFY_TOPIC_OTHER`) — never
commit it to this public repo, since anyone who knows a topic name can read
or spam it.

`state.json` (already-alerted jobs, 72 h memory) is carried between runs via
`actions/cache`. A run that starts with no state records every job it sees
without alerting, so a lost cache never re-spams old postings.
