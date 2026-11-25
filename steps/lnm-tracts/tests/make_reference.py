"""Generate the synthetic reference dataset of the tract-wise disconnection step.

Nothing here is real data. A toy tract atlas of curved streamline bundles (MRtrix .tck, MNI
millimetre coordinates) and a few synthetic lesions on the 2 mm working grid are generated
from fixed seeds; nothing is committed. The expected per-tract values are computed with
plain numpy, independently of MRtrix: a streamline is disconnected when one of its vertices
rounds to a lesion voxel (the rule of ``tckedit -exclude`` with a mask image).

    python steps/lnm-tracts/tests/make_reference.py CONTRACTS_DIR [DEST_DIR]

Written under DEST_DIR (default: tests/reference next to this file):

    lesion_mask.nii.gz                    the reference-test case ("main")
    expected/tract_disconnection.tsv      expected table of "main"
    bundle/                               the toy atlas bundle (bundle.yaml, labels.txt, *.tck)
    cases/<name>.nii.gz                   all lesions, "main" included
    cases/expected/<name>.tsv             expected tables
    legacy_project/data/...               the directory tree lnm_tracts.sh expects
    space/atlas/                          the toy atlas declaring metadata.space ToyAtlasSpace
    space/identity/                       space-transform bundle, identity on the 2 mm grid
    space/shift/                          space-transform bundle, every position + 4 mm in x
    space/mismatch/                       space-transform bundle to another space
    space/expected/<transform>_<case>.tsv expected tables with the atlas in ToyAtlasSpace

The space/ bundles exercise the atlas-space resampling of step 1.1.0 (synthetic spaces:
no real template is involved). With the identity transform the lesion is unchanged, so the
expected tables are those of cases/expected. With the shift transform the resampled lesion
is the lesion moved by 4 mm towards -x; the lesions are balls, so the expected tables are
computed from balls whose centres are moved by 4 mm (two voxels), independently of the
step's resampling code.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml
from nibabel.streamlines import TckFile, Tractogram

SEED = 20261113
N_POINTS = 50
HEADER = ("tract", "disconnection", "n_streamlines", "n_disconnected")
BUNDLE_ID, BUNDLE_VERSION = "toy-lnm-tract-atlas", "1.0.0"
LESION_SPACE, TOY_SPACE = "MNI152NLin6Asym", "ToyAtlasSpace"
SHIFT_MM = (4.0, 0.0, 0.0)
# centre (mm) and radius of the ball lesions; these two are also used shifted
BALLS = {"main": ((-20, -15, 30), 10.0), "small": ((35, -20, -3), 5.0)}


def working_grid(contracts_dir: Path) -> tuple[tuple[int, ...], np.ndarray]:
    registry = yaml.safe_load(
        (contracts_dir / "artefact-types.yaml").read_text(encoding="utf-8")
    )
    grid = registry["grids"]["MNI152NLin6Asym-res2"]
    return tuple(grid["shape"]), np.array(grid["affine"], dtype=float)


# --- tracts: base paths in mm, one function of t in [0, 1] each -----------------------


def _cst(side: float):
    def base(t):
        return np.stack(
            [side * (20 + 3 * np.sin(np.pi * t)), np.full_like(t, -20), -30 + 90 * t], 1
        )

    return base


def _cc(t):
    x = -45 + 90 * t
    return np.stack([x, np.full_like(t, -10), 25 + 20 * (1 - (x / 45) ** 2)], 1)


def _arcuate(t):
    return np.stack([np.full_like(t, -45), 20 - 70 * t, 10 + 25 * np.sin(np.pi * t)], 1)


def _ifof(t):
    return np.stack([35 + 5 * t, -80 + 130 * t, -5 + 8 * np.sin(np.pi * t)], 1)


# name, base path, number of streamlines (different counts give non-trivial fractions)
TRACTS = [
    ("toy_arcuate_left", _arcuate, 50),
    ("toy_cc_body", _cc, 80),
    ("toy_cst_left", _cst(-1.0), 60),
    ("toy_cst_right", _cst(1.0), 60),
    ("toy_ifof_right", _ifof, 45),
]


def _nudge_off_voxel_boundaries(points: np.ndarray, affine: np.ndarray) -> np.ndarray:
    """Move vertices within 0.05 voxel of a rounding tie, so that numpy and MRtrix cannot
    disagree on a tie."""
    inv = np.linalg.inv(affine)
    vox = points @ inv[:3, :3].T + inv[:3, 3]
    frac = vox - np.floor(vox)
    shift_vox = np.where(np.abs(frac - 0.5) < 0.05, 0.1, 0.0)
    return points + shift_vox @ affine[:3, :3].T


def make_streamlines(affine: np.ndarray) -> dict[str, list[np.ndarray]]:
    rng = np.random.default_rng(SEED)
    t = np.linspace(0, 1, N_POINTS)
    out = {}
    for name, base, n in TRACTS:
        path = base(t)
        lines = []
        for _ in range(n):
            offset = rng.normal(0, 3.0, 3)
            phase = rng.uniform(0, 2 * np.pi, 3)
            wiggle = np.sin(2 * np.pi * t[:, None] * 2 + phase) * 1.0
            pts = _nudge_off_voxel_boundaries(path + offset + wiggle, affine)
            lines.append(pts.astype(np.float32))
        out[name] = lines
    return out


def write_tck(lines: list[np.ndarray], path: Path) -> None:
    TckFile(Tractogram(streamlines=lines, affine_to_rasmm=np.eye(4))).save(str(path))


# --- lesions ---------------------------------------------------------------------------


def _voxel_of_mm(affine: np.ndarray, mm) -> np.ndarray:
    inv = np.linalg.inv(affine)
    return inv[:3, :3] @ np.asarray(mm, float) + inv[:3, 3]


def ball(shape, affine, centre_mm, radius_mm) -> np.ndarray:
    ii, jj, kk = np.indices(shape)
    xyz = np.stack([ii, jj, kk], -1) @ affine[:3, :3].T + affine[:3, 3]
    dist = np.linalg.norm(xyz - np.asarray(centre_mm, float), axis=-1)
    return (dist <= radius_mm).astype(np.uint8)


def lesions(shape, affine) -> dict[str, np.ndarray]:
    slab = np.zeros(shape, np.uint8)
    k0 = int(round(_voxel_of_mm(affine, (0, 0, 10))[2]))
    slab[:, :, k0 : k0 + 2] = 1  # two voxels thick: crosses every vertical tract
    corner = np.zeros(shape, np.uint8)
    corner[3:8, 3:8, 3:8] = 1  # far from every tract
    return {
        "main": ball(shape, affine, *BALLS["main"]),
        "small": ball(shape, affine, *BALLS["small"]),
        "wide": slab,
        "none": corner,
    }


def expected_table(
    streamlines: dict[str, list[np.ndarray]], mask: np.ndarray, affine: np.ndarray
) -> str:
    inv = np.linalg.inv(affine)
    rows = ["\t".join(HEADER)]
    for name, _, _ in TRACTS:
        lines = streamlines[name]
        cut = 0
        for pts in lines:
            vox = np.rint(pts.astype(np.float64) @ inv[:3, :3].T + inv[:3, 3]).astype(int)
            inside = np.all((vox >= 0) & (vox < np.array(mask.shape)), axis=1)
            v = vox[inside]
            cut += int(np.any(mask[v[:, 0], v[:, 1], v[:, 2]] > 0))
        rows.append(f"{name}\t{cut / len(lines):.6f}\t{len(lines)}\t{cut}")
    return "\n".join(rows) + "\n"


# --- bundle and legacy tree --------------------------------------------------------------


def write_bundle(
    bundle: Path,
    streamlines: dict[str, list[np.ndarray]],
    bundle_id: str = BUNDLE_ID,
    space: str | None = None,
) -> None:
    bundle.mkdir(parents=True, exist_ok=True)
    names = [t[0] for t in TRACTS]
    (bundle / "labels.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    for name in names:
        write_tck(streamlines[name], bundle / f"{name}.tck")
    metadata = {"n_tracts": len(names)}
    if space is not None:
        metadata["space"] = space
    write_manifest(
        bundle,
        bundle_id=bundle_id,
        bundle_type="tract-atlas",
        title="Toy tract atlas for the tract-wise disconnection step (synthetic)",
        description=(
            "Five synthetic streamline bundles (.tck) with labels.txt, in "
            f"{space or 'MNI152NLin6Asym'} millimetre coordinates. For CI and demos only."
        ),
        metadata=metadata,
    )


def write_manifest(
    bundle: Path, bundle_id: str, bundle_type: str, title: str, description: str, metadata: dict
) -> None:
    """bundle.yaml listing every other file of ``bundle`` (bundles.md sections 2 and 3)."""
    files = []
    for p in sorted(bundle.iterdir(), key=lambda q: q.name.encode()):
        if p.name == "bundle.yaml":
            continue
        data = p.read_bytes()
        files.append(
            {
                "path": p.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    lines = "".join(f"{f['sha256']}  {f['path']}\n" for f in files)
    doc = {
        "contract_version": "1.0",
        "kind": "Bundle",
        "id": bundle_id,
        "version": BUNDLE_VERSION,
        "type": bundle_type,
        "title": title,
        "description": description,
        "synthetic": True,
        "licence": {
            "spdx": "CC0-1.0",
            "redistributable": True,
            "notes": "Generated data; no public ingredient.",
        },
        "metadata": metadata,
        "content_digest": "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest(),
        "files": files,
    }
    (bundle / "bundle.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def write_legacy_project(dest: Path, bundle: Path, cases: Path, names: list[str]) -> None:
    """The tree lnm_tracts.sh reads: data/yeh_tracts/{labels.txt,*.tck},
    data/lesion/<id>.nii.gz and data/lesion_list.txt."""
    data = dest / "data"
    tracts = data / "yeh_tracts"
    (data / "lesion").mkdir(parents=True, exist_ok=True)
    tracts.mkdir(parents=True, exist_ok=True)
    for p in bundle.iterdir():
        if p.name != "bundle.yaml":
            shutil.copyfile(p, tracts / p.name)
    for n in names:
        shutil.copyfile(cases / f"{n}.nii.gz", data / "lesion" / f"{n}.nii.gz")
    (data / "lesion_list.txt").write_text("\n".join(names) + "\n", encoding="utf-8")


def write_space_transform(
    bundle: Path, bundle_id: str, to_space: str, shape, affine: np.ndarray, shift_mm
) -> None:
    """A space-transform bundle on the 2 mm working grid: every voxel centre maps to its own
    position plus ``shift_mm`` (the identity for a zero shift)."""
    bundle.mkdir(parents=True, exist_ok=True)
    ii, jj, kk = np.indices(shape)
    centres = np.stack([ii, jj, kk], -1) @ affine[:3, :3].T + affine[:3, 3]
    coords = (centres + np.asarray(shift_mm, float)).astype(np.float32)
    nib.save(nib.Nifti1Image(coords, affine), str(bundle / "coordinate_map.nii.gz"))
    write_manifest(
        bundle,
        bundle_id=bundle_id,
        bundle_type="space-transform",
        title=f"Synthetic transform {LESION_SPACE} to {to_space}, shift {list(shift_mm)} mm",
        description="Coordinate map on the 2 mm working grid. For CI only.",
        metadata={"from_space": LESION_SPACE, "to_space": to_space, "shift_mm": list(shift_mm)},
    )


def generate_space(dest: Path, streamlines, shape, affine, tables: dict) -> None:
    root = dest / "space"
    (root / "expected").mkdir(parents=True, exist_ok=True)
    write_bundle(root / "atlas", streamlines, "toy-lnm-tract-atlas-toyspace", TOY_SPACE)
    write_space_transform(root / "identity", "toy-xfm-identity", TOY_SPACE, shape, affine, (0, 0, 0))
    write_space_transform(root / "shift", "toy-xfm-shift-x4", TOY_SPACE, shape, affine, SHIFT_MM)
    write_space_transform(root / "mismatch", "toy-xfm-other", "OtherSpace", shape, affine, (0, 0, 0))
    for case, (centre, radius) in BALLS.items():
        (root / "expected" / f"identity_{case}.tsv").write_text(tables[case], encoding="utf-8")
        moved = np.asarray(centre, float) - np.asarray(SHIFT_MM)
        table = expected_table(streamlines, ball(shape, affine, moved, radius), affine)
        (root / "expected" / f"shift_{case}.tsv").write_text(table, encoding="utf-8")


def generate(dest: Path, contracts_dir: Path) -> dict:
    shape, affine = working_grid(contracts_dir)
    streamlines = make_streamlines(affine)
    masks = lesions(shape, affine)
    (dest / "cases" / "expected").mkdir(parents=True, exist_ok=True)
    (dest / "expected").mkdir(parents=True, exist_ok=True)
    write_bundle(dest / "bundle", streamlines)
    tables = {}
    for name, mask in masks.items():
        nib.save(nib.Nifti1Image(mask, affine), str(dest / "cases" / f"{name}.nii.gz"))
        tables[name] = expected_table(streamlines, mask, affine)
        (dest / "cases" / "expected" / f"{name}.tsv").write_text(tables[name], encoding="utf-8")
    shutil.copyfile(dest / "cases" / "main.nii.gz", dest / "lesion_mask.nii.gz")
    shutil.copyfile(
        dest / "cases" / "expected" / "main.tsv", dest / "expected" / "tract_disconnection.tsv"
    )
    write_legacy_project(dest / "legacy_project", dest / "bundle", dest / "cases", list(masks))
    generate_space(dest, streamlines, shape, affine, tables)
    return tables


def main() -> None:
    contracts_dir = Path(sys.argv[1])
    default = Path(__file__).resolve().parent / "reference"
    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else default
    tables = generate(dest, contracts_dir)
    print(json.dumps({k: v.splitlines() for k, v in tables.items()}, indent=1))


if __name__ == "__main__":
    main()
