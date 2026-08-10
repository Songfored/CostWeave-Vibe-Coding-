# v0.6.4 benchmark card

The built-in deterministic fault-injection fixture evaluates seven recovery
actions after a rejected node and an accepted reusable artifact.

| Measurement | Fixture result |
| --- | ---: |
| Selected action | `SWITCH_MODEL` |
| Dynamic selected CPS | `3.4947696061` |
| Fixed retry CPS | `5.8339608771` |
| Fixed full-replan CPS | `4.0965841626` |
| Reported recovery advantage | `0.60181456` |
| Provider/network calls | `0 / 0` |

These are deterministic E0 fixture outputs, not real-provider or real-user
measurements. The v0.6.4 exit gate still requires fault-injection coverage plus
real failure samples before any production or commercial claim.
