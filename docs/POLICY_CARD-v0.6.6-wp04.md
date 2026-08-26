# v0.6.6 V066-WP04 policy card

- Network admission: default off.
- Capacity admission: default off.
- Missing or stale telemetry: reject.
- Complete telemetry: mandatory and non-disableable.
- Snapshot clock check: reject stale or materially future `captured_at`.
- Streaming admission: evaluated when provider I/O starts.
- Remote endpoints: rejected.
- Credentials in endpoint URL: rejected.
- Queue, slot and estimated-wait limits: hard constraints.
- Probe authority: caller-injected only.
- Production route mutation: none.
- Commercial claims: not allowed.
