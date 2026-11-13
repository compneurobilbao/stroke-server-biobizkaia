# Licences of fixture ingredients

| Ingredient | Used? | Licence / status |
|---|---|---|
| Generator code (`fixtures/*.py`) | yes | Same licence as this repository. |
| Generated data in `fixtures/data/` | yes | CC0-1.0. Produced from fixed seeds and closed-form geometry. |
| Head phantom, ellipsoid lesions, Voronoi parcellation, streamlines, connectivity matrices | yes | Original and synthetic; no external ingredient. |
| MNI152NLin6Asym 2 mm grid geometry (shape and affine only) | yes | Geometry constants only, taken from the artefact-type registry; no template image is included. |
| Public high-resolution CT brain template (as used by the CT-to-MNI152 step) | no | Not used and not committed. The licence terms for redistributing the template image could not be confirmed as permitting redistribution (the step's own repository is MIT-licensed, which covers its code, not necessarily the template). Decision: fully synthetic phantom instead. If it is later adopted, download it at generation time from its canonical URL with a pinned SHA-256 and record the licence here. |
| Public parcellations (atlas images) | no | Not used; a synthetic parcellation is generated instead, so no atlas licence applies. |
| Public lesion datasets | no | Not used. |
| Python libraries (numpy, nibabel, PyYAML) | tool only | BSD-3-Clause, MIT and MIT respectively; nothing is redistributed. |

Any future public ingredient must be added to this table with its licence, source URL and
checksum before it is used by the generator.
