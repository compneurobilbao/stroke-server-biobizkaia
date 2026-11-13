# External Processor

This folder represents the place where the external neuroimaging processor lives, or from which it is mounted.

## Responsibility

- Host the development dummy processor.
- Document how to connect an external processor without coupling it to the backend.
- Make it clear that the processing algorithm is not part of this initial platform.

## Included Code

- `dummy_processor.py`: development tool to test the whole flow without having the real external processor yet.
- `README.md`: this explanation.

## `dummy_processor.py`

The dummy script receives `--input`, `--output` and `--study-id`. It waits a few seconds and writes a minimal valid PDF to the output directory.

It implements no image analysis, interprets no results and must not be used for medical decisions.

Compatible contract:

```bash
python external_processor/dummy_processor.py --input data/studies/<id>/input --output data/studies/<id>/output --study-id <id>
```

To replace it with the real script, set `PROCESSOR_COMMAND` in `.env`, keeping the placeholders `{input_dir}`, `{output_dir}`, `{study_id}` and optionally `{logs_dir}`.
