# Data

Esta carpeta es el punto de montaje local para datos generados por la aplicación.

## Responsabilidad

- Guardar estudios subidos.
- Guardar estructura BIDS preparada por estudio.
- Guardar resultados del procesador, PDF técnico y ZIP.
- Guardar logs técnicos del procesador.
- Mantener metadatos locales por estudio.

## Estructura Esperada

```text
data/
  studies/
    {study_id}/
      input/original/
      bids_project/data/sub-XXXX/anat/
      runtime_project/
      output/
        Preproc/
        rendered_png/
        reports/technical_report.pdf
        outputs.zip
      logs/
      metadata.json
```

## Qué Se Versiona

Solo se versionan `.gitkeep` y este `README.md`. Los estudios reales, resultados y logs están ignorados por Git.

## Advertencia

No guardar datos identificativos. La versión actual asume estudios anonimizados y aplica la retención inicial por CLI/configuración, no desde la GUI; el borrado real está desactivado por defecto.
