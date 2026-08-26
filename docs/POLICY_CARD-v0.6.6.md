# v0.6.6 policy card

- Network calls: disabled by default.
- Provider calls: disabled by default.
- Paid calls: disabled by default.
- Prompt telemetry storage: disabled by default.
- Core contract provider-specific fields: forbidden.
- Route mutation and online learning: not provided.
- Unknown fields and secret-like metadata: rejected.
- Runtime persistence migration: none; v6.5.0/035 remains the compatibility
  baseline.

Any later adapter must be introduced as a separate, explicitly authorized work
package with a budget, data-residency policy, cancellation behavior and
independent acceptance evidence.
