# v0.6.6 V066-WP04 data card

The fixture contains synthetic `LocalCapacitySnapshot` values and an
in-process fake transport. No user prompts, credentials, real model outputs,
real queue telemetry or provider billing records are included. Evidence level
is E0. Any real capacity or cost evaluation must preserve snapshot freshness,
hardware, model revision, queue source and independent outcome labels. The
collector must emit a timezone-aware `captured_at`; the runtime independently
derives age from that timestamp and rejects material future clock skew.
