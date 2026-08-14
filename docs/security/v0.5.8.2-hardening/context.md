# v0.5.8.2 Security Hardening Evidence Context

Source root: `C:/Users/asus/Documents/Codex/2026-07-16/w`  
Source revision: unavailable (workspace is not a Git worktree)  
Source drift: unknown  
Collection SHA-256: `fab1b89c7d4de5941c849e04e9b9554f56c5920178711fd18981cf643634c624`

| Evidence | Reader-facing title | Path | SHA-256 |
| --- | --- | --- | --- |
| E001 | Ambient Adapter secret and network authority | `costweave/adapters/http.py` | `bd339d4de8717db19d3becf538e74b09ea80d552a434938a3f2bdbc410b94c1b` |
| E002 | External-call reservation and retry state | `costweave/experimental_calls.py` | `77fe72c4304b8b153a818330e3c2b3dcbcce54934c1e2fd1de83f958b9abff94` |
| E003 | Local export redaction rules | `costweave/evidence_console.py` | `843ee8d80540ba4c47e449d930bbca5f7bd85dd191c89861bbed805035b12daa` |
| E004 | Unscoped local HTTP read and write surface | `costweave/server.py` | `25644c947df2a9c34dac792eed61467c6e2e406da525644f4bb986277f82dc68` |
| E005 | Run request lacks security scope | `costweave/domain.py` | `f1e3b8a7ea0bb056f7ff81a370049190a47f2307bb184df50907528ffd8c06da` |
| E006 | Run manager exposes global collections | `costweave/engine.py` | `9deb3f5e9cfa20518ddba972ce4763c0051da08b84092ef513609419a75d7990` |
| E007 | Runtime migration and projection owner | `costweave/event_store.py` | `1e0ec801a5c282171c5b776a24f2853a635d4a886c47ea5f6324fe8bb4d276f6` |

The evidence is source review, not a vulnerability scan or production measurement. No immutable repository revision was available, so implementation must refresh these boundaries before release.
