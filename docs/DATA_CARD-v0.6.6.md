# v0.6.6 data card

## Data handled by the contract

The in-memory contract validates bounded messages, structured output, tool
schemas, usage, billing, status, errors, cancellation and correlation IDs.
Serialized examples may contain user-provided content, so applications must
apply their own data-residency and retention policies.

## Data not collected by this work package

No prompt corpus, provider response, credential, endpoint, network trace or
training record is persisted by the gateway contract itself. Secret-like
metadata is rejected at the boundary.

## Evidence quality

Bundled tests are E0 simulated/offline engineering evidence. They are not
representative provider traffic and cannot support cost or quality claims.
