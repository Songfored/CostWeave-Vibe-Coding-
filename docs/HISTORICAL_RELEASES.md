# Historical releases

This index records the provenance of the tags rebuilt for the public Git
repository on 2026-08-27. It keeps the public source repository clean: source
snapshots are represented by commits and tags, while wheel/zip artifacts belong
in GitHub Releases rather than `main`.

## Tagging rules

- A normal tag points to a preserved source snapshot.
- `runtime reconstructed` means the original wheel was available but the source
  distribution was not. The tag contains the wheel's unpacked runtime source and
  a provenance note; it does not claim to reproduce the original documentation.
- Work-package tags are historical engineering milestones, not semantic public
  releases.
- Corrected packaging copies are not separate feature tags when a final snapshot
  exists; the final corrected snapshot is used.

## Formal releases

| Tag range | Provenance |
|---|---|
| v0.2, v0.3, v0.4.1 | Preserved source archives |
| v0.4.2, v0.4.3 | Runtime reconstructed from preserved wheels |
| v0.4.4–v0.4.9 | Preserved source archives |
| v0.5.1–v0.5.6.1 | Preserved source archives |
| v0.5.6.2 | Runtime reconstructed from preserved wheel |
| v0.5.6.3–v0.5.8.4 | Preserved source archives |
| v0.6.0–v0.6.6 | Preserved source archives; v0.6.5 uses the final corrected source snapshot |
| v0.6.7 | Preserved final staging source snapshot |
| v0.6.8, v0.6.9 | Preserved release staging snapshots |

## Engineering milestones

| Tag | Provenance |
|---|---|
| v0.6.6-wp02 | Preserved source archive |
| v0.6.6-wp03 | Final clean corrected source snapshot |
| v0.6.6-wp04 | Final corrected source staging snapshot |
| v0.6.6-wp05 | Final corrected release source snapshot |
| v0.6.6-wp06 | Final release source staging snapshot |
| v0.6.6-wp07 | Final release source staging snapshot |

## What is intentionally excluded from Git

Generated wheels, zip files, unpacked test environments, logs, caches, build
directories, local databases, transient `work/` folders and model credentials
are excluded by `.gitignore`. They may be attached to a selected GitHub Release
after their checksums and acceptance report have been verified.
