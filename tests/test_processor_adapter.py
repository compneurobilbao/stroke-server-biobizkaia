import json
import struct
import sys
import zlib
from pathlib import Path

from processor_adapter import CompneuroAnatprocAdapter, ProcessorAdapter
from processor_adapter.adapter import processor_environment
from processor_adapter.artifacts import write_outputs_zip, write_technical_pdf
from processor_adapter.nifti_renderer import (
    build_slicer_command,
    find_nifti_files,
    render_nifti_outputs,
)
from processor_adapter.technical_pdf_report import (
    TechnicalReportMetadata,
    write_technical_pdf_report,
)


def test_adapter_detects_generated_pdf(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir.mkdir()
    (input_dir / "study.nii").write_text("dummy", encoding="utf-8")
    script = Path("external_processor/dummy_processor.py").resolve()
    adapter = ProcessorAdapter(
        f"python {script} --input {{input_dir}} --output {{output_dir}} --study-id {{study_id}} --sleep 0"
    )

    result = adapter.run(input_dir, output_dir, "abc", logs_dir)

    assert result.success is True
    assert result.exit_code == 0
    assert result.pdf_path is not None
    assert result.pdf_path.exists()
    assert result.log_path.exists()


def test_adapter_reports_failed_script(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir.mkdir()
    script = Path("external_processor/dummy_processor.py").resolve()
    adapter = ProcessorAdapter(
        f"python {script} --input {{input_dir}} --output {{output_dir}} --study-id {{study_id}} --sleep 0 --fail"
    )

    result = adapter.run(input_dir, output_dir, "abc", logs_dir)

    assert result.success is False
    assert result.exit_code != 0
    assert result.error_message == "The external processor finished with an error"


def test_adapter_fails_without_pdf(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir.mkdir()
    adapter = ProcessorAdapter("python -c 'print(123)'")

    result = adapter.run(input_dir, output_dir, "abc", logs_dir)

    assert result.success is False
    assert result.error_message == "The processor did not generate any PDF"


def test_compneuro_adapter_detects_preproc_outputs(tmp_path):
    bids_project_dir = tmp_path / "bids_project"
    data_dir = bids_project_dir / "data"
    anat_dir = data_dir / "sub-O01" / "anat"
    anat_dir.mkdir(parents=True)
    (anat_dir / "sub-O01_T1w.nii.gz").write_bytes(b"nifti")
    (data_dir / "participants.tsv").write_text(
        "participant_id\nsub-O01\n", encoding="utf-8"
    )
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    runtime_project_dir = tmp_path / "runtime_project"
    project_mount = tmp_path / "project"
    adapter = CompneuroAnatprocAdapter(
        "mkdir -p {project_dir}/Preproc/BET {project_dir}/Preproc/ProbTissue && "
        "touch {project_dir}/Preproc/BET/sub-O01_T1w_brain.nii.gz "
        "{project_dir}/Preproc/ProbTissue/sub-O01_T1w_brain_CSF.nii.gz",
        project_mount=project_mount,
    )

    result = adapter.run(
        input_dir=tmp_path / "input",
        output_dir=output_dir,
        study_id="abc",
        logs_dir=logs_dir,
        bids_project_dir=bids_project_dir,
        runtime_project_dir=runtime_project_dir,
    )

    assert result.success is True
    assert result.exit_code == 0
    assert (output_dir / "Preproc" / "BET").is_dir()
    assert (output_dir / "Preproc" / "ProbTissue").is_dir()
    assert len(result.output_files) == 2


WORKER_SECRETS = {
    "DATABASE_URL": "postgresql+psycopg://u:p@db/x",
    "CELERY_BROKER_URL": "redis://:pw@redis:6379/0",
    "SMTP_PASSWORD": "smtp-secret",
    "AUTH_SECRET_KEY": "k" * 40,
    "REDIS_PASSWORD": "redis-secret",
}
TOOL_VARIABLES = {
    "FSLDIR": "/opt/fsl",
    "FSLOUTPUTTYPE": "NIFTI_GZ",
    "ANTSPATH": "/opt/ants/",
    "FREESURFER_HOME": "/opt/fs",
    "FS_LICENSE": "/opt/fs/license.txt",
    "MNI_DIR": "/opt/fs/mni",
    "PERL5LIB": "/opt/fs/mni/share/perl5",
}


def test_processor_environment_is_an_allowlist():
    source = {"PATH": "/bin", "HOME": "/root", "LANG": "C.UTF-8"}
    source.update(WORKER_SECRETS)
    source.update(TOOL_VARIABLES)
    source["UNRELATED"] = "x"

    env = processor_environment(source)

    assert env == {"PATH": "/bin", "HOME": "/root", "LANG": "C.UTF-8", **TOOL_VARIABLES}


def test_processor_environment_passthrough_adds_named_variables():
    source = {
        "PATH": "/bin",
        "PROCESSOR_ENV_PASSTHROUGH": " MY_TOOL_HOME , ,OTHER",
        "MY_TOOL_HOME": "/opt/tool",
        "DATABASE_URL": "x",
    }

    env = processor_environment(source)

    assert env == {"PATH": "/bin", "MY_TOOL_HOME": "/opt/tool"}


def _dump_env_command(target: Path) -> str:
    code = "import json, os, sys; json.dump(dict(os.environ), open(sys.argv[1], 'w'))"
    return f'"{sys.executable}" -c "{code}" {target}'


def test_dummy_adapter_does_not_pass_worker_secrets(tmp_path, monkeypatch):
    for name, value in {**WORKER_SECRETS, **TOOL_VARIABLES}.items():
        monkeypatch.setenv(name, value)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    env_file = tmp_path / "env.json"
    adapter = ProcessorAdapter(_dump_env_command(env_file))

    adapter.run(input_dir, tmp_path / "output", "abc", tmp_path / "logs")

    env = json.loads(env_file.read_text())
    for name in WORKER_SECRETS:
        assert name not in env
    for name, value in TOOL_VARIABLES.items():
        assert env[name] == value
    assert "PATH" in env


def test_compneuro_adapter_does_not_pass_worker_secrets(tmp_path, monkeypatch):
    for name, value in {**WORKER_SECRETS, **TOOL_VARIABLES}.items():
        monkeypatch.setenv(name, value)
    bids_project_dir = tmp_path / "bids_project"
    (bids_project_dir / "data").mkdir(parents=True)
    env_file = tmp_path / "env.json"
    project_mount = tmp_path / "project"
    adapter = CompneuroAnatprocAdapter(
        _dump_env_command(env_file), project_mount=project_mount
    )

    adapter.run(
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        study_id="abc",
        logs_dir=tmp_path / "logs",
        bids_project_dir=bids_project_dir,
        runtime_project_dir=tmp_path / "runtime_project",
    )

    env = json.loads(env_file.read_text())
    for name in WORKER_SECRETS:
        assert name not in env
    for name, value in TOOL_VARIABLES.items():
        assert env[name] == value
    assert env["PROJECT_PATH"] == str(project_mount)


def test_compneuro_adapter_fails_if_project_mount_is_not_managed(tmp_path):
    bids_project_dir = tmp_path / "bids_project"
    (bids_project_dir / "data").mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    project_mount = tmp_path / "project"
    project_mount.mkdir()
    adapter = CompneuroAnatprocAdapter("true", project_mount=project_mount)

    result = adapter.run(
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        study_id="abc",
        logs_dir=logs_dir,
        bids_project_dir=bids_project_dir,
        runtime_project_dir=tmp_path / "runtime_project",
    )

    assert result.success is False
    assert result.error_message == "Could not prepare the /project environment"


def test_technical_pdf_and_zip_are_generated(tmp_path):
    preproc_dir = tmp_path / "Preproc"
    (preproc_dir / "BET").mkdir(parents=True)
    (preproc_dir / "ProbTissue").mkdir()
    output_file = preproc_dir / "BET" / "sub-O01_T1w_brain.nii.gz"
    output_file.write_bytes(b"nifti")

    pdf_path = write_technical_pdf(
        tmp_path / "logs" / "technical_report.pdf",
        study_id="study-1",
        bids_subject_id="sub-O01",
        processor_name="compneuro-anatproc",
        processor_version="1.1",
        processor_backend="compneuro",
        status="completed",
        output_files=[output_file],
    )
    zip_path = write_outputs_zip(preproc_dir, tmp_path / "outputs.zip")

    assert pdf_path.exists()
    assert zip_path.exists()


def test_find_nifti_files_only_returns_nifti_from_preproc(tmp_path):
    preproc_dir = tmp_path / "Preproc"
    (preproc_dir / "BET").mkdir(parents=True)
    (preproc_dir / "ProbTissue").mkdir()
    expected_nii = preproc_dir / "BET" / "sub-O01_brain.nii"
    expected_niigz = preproc_dir / "ProbTissue" / "sub-O01_CSF.nii.gz"
    expected_nii.write_bytes(b"nii")
    expected_niigz.write_bytes(b"niigz")
    (preproc_dir / "BET" / "notes.txt").write_text("ignored", encoding="utf-8")
    (preproc_dir / "BET" / "sub-O01_tmp.nii.gz").write_bytes(b"ignored")

    assert find_nifti_files(preproc_dir) == [expected_nii, expected_niigz]


def test_build_slicer_command_uses_configured_renderer(tmp_path):
    command = build_slicer_command(
        "slicer", tmp_path / "input.nii.gz", tmp_path / "output.png"
    )

    assert command == [
        "slicer",
        str(tmp_path / "input.nii.gz"),
        "-a",
        str(tmp_path / "output.png"),
    ]


def test_render_nifti_outputs_uses_runner_without_requiring_fsl(tmp_path):
    preproc_dir = tmp_path / "Preproc"
    preproc_dir.mkdir()
    nifti_path = preproc_dir / "sub-O01_T1w.nii.gz"
    nifti_path.write_bytes(b"nifti")

    def fake_runner(args, **kwargs):
        Path(args[3]).write_bytes(_png_bytes())
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    rendered = render_nifti_outputs(
        preproc_dir, tmp_path / "output" / "rendered_png", runner=fake_runner
    )

    assert len(rendered) == 1
    assert rendered[0].success is True
    assert rendered[0].png_path.exists()


def test_render_nifti_outputs_records_failed_render(tmp_path):
    preproc_dir = tmp_path / "Preproc"
    preproc_dir.mkdir()
    (preproc_dir / "sub-O01_T1w.nii.gz").write_bytes(b"nifti")

    def fake_runner(args, **kwargs):
        return type("Completed", (), {"returncode": 2, "stderr": "forced"})()

    rendered = render_nifti_outputs(
        preproc_dir, tmp_path / "rendered_png", runner=fake_runner
    )

    assert rendered[0].success is False
    assert rendered[0].error_message == "slicer exited with code 2"


def test_technical_pdf_report_contains_nifti_names_and_png(tmp_path):
    png_path = tmp_path / "rendered.png"
    png_path.write_bytes(_png_bytes())
    nifti_path = tmp_path / "Preproc" / "BET" / "sub-O01_T1w_brain.nii.gz"
    artifact = type(
        "Artifact",
        (),
        {
            "nifti_path": nifti_path,
            "png_path": png_path,
            "display_name": "BET/sub-O01_T1w_brain.nii.gz",
            "success": True,
            "error_message": None,
        },
    )()
    pdf_path = write_technical_pdf_report(
        tmp_path / "output" / "reports" / "technical_report.pdf",
        metadata=TechnicalReportMetadata(
            study_id="study-1",
            study_name="study.nii.gz",
            bids_subject_id="sub-O01",
            uploaded_at=None,
            processed_at=None,
            pipeline_name="compneuro-anatproc",
            pipeline_version="1.1",
            processor_backend="compneuro",
            logical_output_path="data/studies/study-1/output",
        ),
        rendered_outputs=[artifact],
        output_files=[nifti_path],
    )

    pdf_bytes = pdf_path.read_bytes()
    assert b"BET/sub-O01_T1w_brain.nii.gz" in pdf_bytes
    assert b"/Subtype/Image" in pdf_bytes


def test_technical_pdf_report_handles_no_nifti(tmp_path):
    pdf_path = write_technical_pdf_report(
        tmp_path / "technical_report.pdf",
        metadata=TechnicalReportMetadata(
            study_id="study-1",
            study_name="study.nii.gz",
            bids_subject_id="sub-O01",
            uploaded_at=None,
            processed_at=None,
            pipeline_name="compneuro-anatproc",
            pipeline_version="1.1",
            processor_backend="compneuro",
            logical_output_path="data/studies/study-1/output",
        ),
        rendered_outputs=[],
        output_files=[],
    )

    assert b"No renderable NIfTI files found" in pdf_path.read_bytes()


def _png_bytes() -> bytes:
    width = 2
    height = 2
    raw_rows = b"\x00\xff\x00\x00\x00\xff\x00" * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw_rows))
        + _chunk(b"IEND", b"")
    )


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )
