# V066-WP06 Runtime Backend policy card

- local-first implementation: enabled;
- persistent runtime required: enabled;
- submit/cancel/checkpoint/resume/events: enabled;
- project-scoped event access: mandatory;
- idempotency and optimistic revisions: mandatory;
- third-party runtime mode: adapter-only;
- external runtime SDKs: disabled by default;
- network and paid provider calls: disabled by default;
- automatic production route intervention: not provided;
- commercial claims: prohibited without preregistered real evidence.

The policy is exposed by `public_runtime_backend_policy()` and the
`/api/v1/runtime/status` endpoint.
