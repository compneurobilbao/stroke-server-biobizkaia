"""Research report v0: PDF and canonical JSON from the artefacts of the previous steps.

Required inputs: ``lesion_qc``, ``lesion_mask`` and ``run_provenance``. The other inputs
(``ct_mni``, ``fdisconnection_map``, ``tract_disconnection``, ``cognet_profile``) are
optional: when one is absent its section says so and the run still succeeds.
"""

from __future__ import annotations

import json
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from jsonschema import Draft202012Validator

from stroke_step_sdk import InputRejected, StepContext, contracts
from stroke_step_sdk.hashing import sha256_file

from . import canonical, figures, model, render


def _load_json(ctx: StepContext, port: str) -> dict:
    try:
        doc = json.loads(ctx.input_path(port).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise InputRejected(
            "input_format_invalid", "The input is not valid UTF-8 JSON.", port=port
        ) from None
    if not isinstance(doc, dict):
        raise InputRejected("input_format_invalid", "The input is not a JSON object.", port=port)
    return doc


def _guard(port: str, fn, *args):
    """Run a content check; unusable content is a rejected input, not an internal failure."""
    try:
        return fn(*args)
    except (model.InvalidInput, KeyError, TypeError, IndexError) as exc:
        detail = str(exc) if isinstance(exc, model.InvalidInput) else f"missing field {exc}"
        raise InputRejected("input_format_invalid", detail[:300], port=port) from None


def _cognet(ctx: StepContext) -> dict | None:
    if not ctx.has_input("cognet_profile"):
        return None
    doc = _load_json(ctx, "cognet_profile")
    schema_path = contracts.contracts_dir() / "schemas" / "v1" / "cognet-profile.schema.json"
    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        raise InputRejected(
            "input_format_invalid",
            "The cognitive profile does not satisfy its JSON Schema.",
            port="cognet_profile",
            details={"first_error": errors[0].message[:200], "error_count": len(errors)},
        )
    return doc


def run(ctx: StepContext) -> None:
    language = ctx.params["language"]

    lesion_qc = _load_json(ctx, "lesion_qc")
    run_doc = _load_json(ctx, "run_provenance")
    mask_path = ctx.input_path("lesion_mask")
    mask = _guard("lesion_mask", model.mask_stats, mask_path)
    lesion = _guard("lesion_qc", model.lesion_section, lesion_qc, mask)
    provenance = _guard("run_provenance", model.provenance_section, run_doc)

    rows = None
    if ctx.has_input("tract_disconnection"):
        rows = _guard("tract_disconnection", model.read_tract_table, ctx.input_path("tract_disconnection"))
    tracts = model.tract_section(rows)

    if ctx.has_input("fdisconnection_map"):
        functional = _guard(
            "fdisconnection_map",
            model.fdisconnection_stats,
            ctx.input_path("fdisconnection_map"),
        )
    else:
        functional = {"status": "absent"}

    profile = _cognet(ctx)
    cognet = _guard("cognet_profile", model.cognet_section, profile)

    # digests of the exact files this report was computed from
    inputs = []
    for port in ctx.manifest.inputs:
        present = ctx.has_input(port.name)
        rec = {"port": port.name, "type": port.type, "present": present}
        if present:
            path = ctx.input_path(port.name)
            rec["sha256"] = sha256_file(path)
            rec["size_bytes"] = path.stat().st_size
        inputs.append(rec)
    inputs.sort(key=lambda r: r["port"])

    report = model.build_report(
        language=language,
        step={"name": ctx.manifest.name, "version": ctx.manifest.version},
        run_provenance=provenance,
        inputs=inputs,
        lesion=lesion,
        functional=functional,
        tracts=tracts,
        cognet=cognet,
    )

    ct_mni = ctx.input_path("ct_mni") if ctx.has_input("ct_mni") else None
    fig_uris: dict[str, str | None] = {"lesion": None, "functional": None, "tracts": None}
    with tempfile.TemporaryDirectory(dir=ctx.tmp_dir) as tmp:
        tmp = Path(tmp)
        report_bg = figures.lesion_figure(mask_path, tmp / "lesion.png", ct_mni)
        fig_uris["lesion"] = render.data_uri(tmp / "lesion.png")
        if functional["status"] == "available":
            if figures.disconnection_map_figure(
                ctx.input_path("fdisconnection_map"), mask_path, tmp / "fdis.png", ct_mni
            ):
                fig_uris["functional"] = render.data_uri(tmp / "fdis.png")
        if rows is not None:
            figures.tract_figure(rows, tmp / "tracts.png")
            fig_uris["tracts"] = render.data_uri(tmp / "tracts.png")
        fig_uris["background"] = report_bg

        html = render.render_html(language, report, fig_uris)
        render.write_pdf(html, ctx.output_path("report_pdf"))

    ctx.output_path("report_json").write_bytes(canonical.dump_bytes(report))
    ctx.log.info(
        "report written: language %s, sections %s",
        language,
        ",".join(f"{k}={v}" for k, v in report["sections"].items()),
    )
    for dist in ("WeasyPrint", "Jinja2", "nilearn", "nibabel", "numpy", "matplotlib"):
        try:
            ctx.add_software(dist, version(dist))
        except PackageNotFoundError:  # e.g. WeasyPrint when the PDF writer is stubbed in tests
            ctx.log.info("version of %s not available", dist)
