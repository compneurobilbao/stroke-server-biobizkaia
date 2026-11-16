/**
 * Tiny synthetic NIfTI-1 volumes for the viewer specs (card F0-32). Generated in memory from
 * closed-form shapes, so no image file, real or public, is involved. The grid is a coarse
 * (4 mm) cut-down of the MNI152NLin6Asym box with the same origin convention as the
 * platform's working grid.
 */
const NX = 46;
const NY = 55;
const NZ = 46;
const VOXEL_MM = 4;

type Shape = (x: number, y: number, z: number) => number;

/** Uncompressed single-file NIfTI-1, little endian. `kind` is the NIfTI datatype of the voxels. */
function nifti(kind: 'uint8' | 'float32', shape: Shape): Buffer {
  const bytesPerVoxel = kind === 'uint8' ? 1 : 4;
  const header = Buffer.alloc(352);
  header.writeInt32LE(348, 0); // sizeof_hdr
  [3, NX, NY, NZ, 1, 1, 1, 1].forEach((n, i) => header.writeInt16LE(n, 40 + 2 * i)); // dim
  header.writeInt16LE(kind === 'uint8' ? 2 : 16, 70); // datatype
  header.writeInt16LE(bytesPerVoxel * 8, 72); // bitpix
  [1, VOXEL_MM, VOXEL_MM, VOXEL_MM, 1, 1, 1, 1].forEach((n, i) => header.writeFloatLE(n, 76 + 4 * i)); // pixdim
  header.writeFloatLE(352, 108); // vox_offset
  header.writeFloatLE(1, 112); // scl_slope
  header.writeFloatLE(0, 116); // scl_inter
  header.writeInt16LE(0, 252); // qform_code
  header.writeInt16LE(1, 254); // sform_code
  [-VOXEL_MM, 0, 0, 90].forEach((n, i) => header.writeFloatLE(n, 280 + 4 * i)); // srow_x
  [0, VOXEL_MM, 0, -126].forEach((n, i) => header.writeFloatLE(n, 296 + 4 * i)); // srow_y
  [0, 0, VOXEL_MM, -72].forEach((n, i) => header.writeFloatLE(n, 312 + 4 * i)); // srow_z
  header.write('n+1\0', 344, 'latin1'); // magic

  const data = Buffer.alloc(NX * NY * NZ * bytesPerVoxel);
  let offset = 0;
  for (let z = 0; z < NZ; z++) {
    for (let y = 0; y < NY; y++) {
      for (let x = 0; x < NX; x++) {
        const value = shape(x, y, z);
        if (kind === 'uint8') data.writeUInt8(value, offset);
        else data.writeFloatLE(value, offset);
        offset += bytesPerVoxel;
      }
    }
  }
  return Buffer.concat([header, data]);
}

const dist = (x: number, y: number, z: number, c: [number, number, number]) =>
  Math.hypot(x - c[0], y - c[1], z - c[2]);

/** A "head": a bright ellipsoid with a darker interior (stands in for the CT in MNI space). */
export const syntheticTemplate = (): Buffer =>
  nifti('float32', (x, y, z) => {
    const r = Math.hypot((x - 23) / 20, (y - 30) / 24, (z - 23) / 20);
    return r > 1 ? 0 : r > 0.85 ? 90 : 40;
  });

/** A binary ball in the left hemisphere (image left is at high x in this grid). */
const LESION_CENTRE: [number, number, number] = [33, 30, 23];
export const syntheticLesion = (): Buffer => nifti('uint8', (x, y, z) => (dist(x, y, z, LESION_CENTRE) <= 4 ? 1 : 0));

/** A smooth map around the lesion with values from 0 to 8. */
export const syntheticDisconnection = (): Buffer =>
  nifti('float32', (x, y, z) => 8 * Math.exp(-((dist(x, y, z, LESION_CENTRE) / 8) ** 2)));
