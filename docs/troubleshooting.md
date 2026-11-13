# Troubleshooting

## The GUI Does Not Load

Check `docker compose ps` and the logs of `reverse-proxy` and `frontend`.

## The API Does Not Respond

Check `/api/health`, migrations and the connection to PostgreSQL.

## The Study Stays Queued

Check that `worker` and `redis` are up.

## Processing Fails

Check `data/studies/{study_id}/logs/processor.log` and the value of `PROCESSOR_COMMAND`.

## No PDF

In `dummy` mode, the external script must generate at least one `.pdf` inside the `output` directory.

In `compneuro` mode, the flow does not generate a clinical PDF. The platform generates `output/reports/technical_report.pdf` after detecting results in `output/Preproc` and rendering PNGs into `output/rendered_png/`.

## No Rendered PNGs

Check `GENERATE_RENDERED_PNG=true`, `NIFTI_RENDERER=slicer` and `data/studies/{study_id}/logs/rendering.log`. In the compneuro worker, `slicer` must be available in the `PATH` because it comes from FSL inside the base image.

## No ZIP

Check that `GENERATE_OUTPUT_ZIP=true`, that the study is `completed` and that files exist in `data/studies/{study_id}/output/Preproc`.

## Compneuro Fails On `/project`

`compneuro-anatproc` expects `/project/data/participants.tsv` and writes to `/project/Preproc`. The worker creates a managed symlink to `runtime_project`. If `/project` exists inside the container and is not a managed symlink, processing fails so that external paths are not overwritten.

## Missing Outputs

Compneuro processing is only marked successful if `Preproc/BET` and `Preproc/ProbTissue` exist. If a folder is missing, check `logs/processor.log` and the permissions of the `./data:/app/data` volume.
