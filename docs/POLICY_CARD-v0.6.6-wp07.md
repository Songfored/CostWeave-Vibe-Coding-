# V066-WP07 Small-Budget Shadow policy card

- advisory-only Shadow: mandatory;
- baseline execution continues: mandatory;
- route intervention and auto-promotion: prohibited;
- live calls enabled by default: no;
- paid calls enabled by default: no;
- built-in Provider executor: none;
- explicit Provider/data scope/hard budget: mandatory;
- remote secret reference and current value: mandatory;
- network and paid consent for remote execution: mandatory;
- atomic pre-call budget reservation: mandatory;
- unknown post-dispatch failure cost: charge reserved ceiling conservatively;
- release maximum budget: USD 5;
- release maximum logical calls: 20;
- automatic attempts per probe: exactly 1;
- release maximum output tokens per probe: 8,192;
- maximum session lifetime: 24 hours;
- raw prompt/output/secret persistence: prohibited;
- admitted evidence labels: `E0-fixture`, `E2-real-unvalidated` only;
- project isolation and idempotency: mandatory;
- commercial claims: prohibited at E0.

The policy is exposed by `public_small_budget_shadow_policy()` and
`GET /api/v1/runtime/shadow/status`.
