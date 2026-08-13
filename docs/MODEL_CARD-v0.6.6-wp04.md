# v0.6.6 V066-WP04 model card

CostWeave is not a trained language model. WP04 is a deterministic policy and
adapter contract that guards local Ollama/vLLM-style OpenAI-compatible calls
with a capacity snapshot.

The component does not estimate quality and does not discover local hardware.
It can only enforce the caller-supplied queue, slot, wait and freshness limits.
The static fixture is E0 evidence; no provider quality or production capacity
claim is made.
