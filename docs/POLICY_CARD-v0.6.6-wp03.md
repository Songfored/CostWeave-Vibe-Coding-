# v0.6.6 V066-WP03 policy card

- Gateway enabled by default: no.
- Paid calls enabled by default: no.
- Unclassified model billing mode: `unknown` and treated as paid.
- Paid/unknown request without paid authority: rejected before transport.
- Unexpected transport exception retryable: no.
- LiteLLM SDK imported by core: no.
- Credentials stored by core: no.
- Remote authorization performed by core: no.
- Streaming advertised: no.
- Provider cancellation advertised: no.
- Evidence level: E0 local/fake-gateway.
