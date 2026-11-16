# Reference data: the bundle registry

Status: implemented by card F0-18. Contract: [docs/contracts/bundles.md](contracts/bundles.md); decision: [ADR-0006](adr/0006-reference-bundles-are-data.md). Code: `orchestrator/bundles.py` (library and CLI), `scripts/bundles/registry.py` (CLI entry point), table `reference_bundles` ([data-model.md](data-model.md) 4.9).

Reference data (normative connectomes, tract atlases, templates, model weights) is never in Git. It is stored as immutable, versioned bundles under one **bundle root** per host, registered and verified by the registry, and mounted read-only at `/ref/<requirement name>/` in the step container.

## Layout on a host

```
<bundle root>/<bundle id>/<version>/bundle.yaml
                                    <files listed in bundle.yaml>
<bundle root>/.registry.json        # only with the default JSON store
```

The bundle root is deployment configuration (`--root` or the `BUNDLE_ROOT` environment variable). Mount the root read-only into the API, worker and executors where possible; only the registration command needs write access.

## Preparing a bundle

1. Put the files in a directory and write `bundle.yaml` with `id`, `version`, `type`, `title`, `licence.spdx`, `licence.redistributable` (both required), optional `citation`, `metadata`, `synthetic`, and the `files` list with SHA-256 and size of every file. Compute `content_digest` as specified in bundles.md section 3 (`contracts.validate.bundle_digest`).
2. Check the manifest: `python contracts/validate.py path/to/bundle.yaml`.
3. Register it (below). The registry re-hashes everything itself; a manifest that disagrees with the files is refused.

## Command line

```bash
export BUNDLE_ROOT=/srv/bundles          # or pass --root
python scripts/bundles/registry.py register path/to/bundle-dir
python scripts/bundles/registry.py list [--type normative-connectome] [--status active] [--json]
python scripts/bundles/registry.py verify                      # full re-hash of all bundles
python scripts/bundles/registry.py verify <id> <version> [--quick]
python scripts/bundles/registry.py withdraw <id> <version>
python scripts/bundles/registry.py resolve steps/<step>/step.yaml   # what would be mounted
```

`python -m orchestrator.bundles` is the same command. `--store db` writes the `reference_bundles` table (needs the backend environment, `DATABASE_URL`) instead of `<root>/.registry.json`. Exit status is 1 on any error or failed verification.

What each command guarantees:

- `register` verifies the manifest against the schema (licence required), the `content_digest` formula, and every file's size and SHA-256; it also refuses files on disk that the manifest does not list. It copies the bundle into the root (unless already there) and verifies the copy. Registering the same id and version with different content is refused: versions are immutable. Registering identical content again is a no-op.
- `verify` records `verified` or `failed`. A failed bundle is never resolved until a later full verification passes (for example after re-copying it from the source). Run it after copying a bundle to another host (HPC) and periodically.
- `withdraw` stops a bundle from being used by new runs; past runs keep their reference.

## For executors and the orchestrator

```python
from orchestrator.bundles import BundleRegistry, BundleError, mount_specs

registry = BundleRegistry(bundle_root)            # JSON store; SqlAlchemyStore(session) for the DB
resolved = registry.resolve(step_manifest)        # {name: ResolvedBundle}
for host, container, mode in mount_specs(resolved):   # (host path, "/ref/<name>", "ro")
    ...
```

`step_manifest` is the parsed `step.yaml`; its `bundles` requirements (`name`, `type`, optional `id`, `version` range, `required`) are matched against active, verified bundles, taking the highest matching version unless `selection={name: (id, version)}` pins one (pipelines or deployments may pin). Before returning, each chosen bundle gets the cheap pre-mount check of bundles.md section 4.2 (manifest hash unchanged since registration, every listed file present with the listed size); `full=True` re-hashes, and must be used after a bundle was copied to another host.

The step is refused, with an exception that names every problem, when a required bundle is missing, withdrawn, out of range or fails verification: `BundleNotFoundError` or `BundleVerificationError` (both `BundleError`). The caller must not start the step container then. `ResolvedBundle` carries `bundle_id`, `version` and `content_digest`, which the orchestrator compares with what the step reports in its provenance.

## Limits and open points

- The cheap check does not detect a same-size content change; that needs a full verification (scheduled or forced with `full=True`).
- Bundle layouts per bundle type are defined by the consuming steps. The `tract-atlas` type has the layout the structural-disconnection step reads (`labels.txt` plus one `<tract>.tck` per tract at the bundle root); the synthetic fixture `toy-tract-atlas` uses it.
- Installers and release tarballs must skip bundles whose `licence.redistributable` is `false`.
