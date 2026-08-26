# v0.6.6 V066-WP02 data card

The adapter holds a bounded request in memory long enough to serialize it and
maps the response into typed gateway objects. The in-process Fake Server keeps
request fixtures only for its test process. WP02 adds no SQLite persistence,
prompt corpus or training data.

Applications using a real local endpoint are responsible for the endpoint's
own retention and data-residency behavior. The core rejects endpoint
credentials and does not persist provider secrets.
