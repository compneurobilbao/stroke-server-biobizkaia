# Manual De Operaciones

## Comandos

- `make up`: levantar servicios.
- `make down`: parar servicios.
- `make logs`: inspeccionar logs.
- `make smoke`: comprobar healthcheck.
- `make migrate`: aplicar migraciones Alembic en el contenedor `api`.
- `make create-admin EMAIL=admin@example.org`: crear o actualizar el admin inicial.
- `make backup`: crear backup local de PostgreSQL y `data/studies`.
- `make restore BACKUP_DIR=backups/<timestamp> CONFIRM_RESTORE=YES_I_UNDERSTAND`: restaurar backup local.
- `make frontend-rebuild`: reconstruir y recrear solo el frontend Docker.
- `make up WORKER_REPLICAS=2`: levantar servicios con dos contenedores `worker`.
- `make rebuild`: reconstruir y recrear todos los servicios Docker; acepta `WORKER_REPLICAS=N`.
- `make clean`: limpiar volúmenes y estudios locales.

## Usuarios

Tras levantar servicios, aplicar migraciones antes de crear el primer usuario admin:

```bash
make up
make migrate
```

Después, crear o actualizar el admin inicial:

```bash
make create-admin EMAIL=admin@example.org
```

El comando pide contraseña por consola. No hay registro público abierto; los demás usuarios se crean desde la GUI con una cuenta admin.

## Datos Y Backups

- PostgreSQL guarda metadatos, estados y auditoría.
- `data/studies/{study_id}` guarda entrada, BIDS, resultados, logs y metadatos.
- Respaldar PostgreSQL y `data/`; ambos son necesarios para reconstruir trazabilidad.
- La retención inicial se ejecuta por CLI/configuración y no desde la GUI. El borrado real está desactivado por defecto.

La Fase 4 implementa backup/restore local por CLI, sin endpoints ni pantalla admin:

- `make backup` crea `backups/<timestamp>/db.sql`, `studies.tar.gz` y `manifest.txt`.
- El backup se escribe primero en un directorio temporal `backups/.tmp-*` y solo se publica como `backups/<timestamp>` si termina correctamente.
- `db.sql` se genera con `pg_dump` dentro del contenedor `postgres`.
- `studies.tar.gz` archiva `data/studies` desde un contenedor auxiliar basado en `api`, para poder leer resultados creados como `root` por el procesador.
- Durante el backup se pausan `api` y `worker` para evitar escrituras concurrentes.
- El backup se cancela si existen jobs `queued` o `processing`; primero hay que esperar, cancelar o resolver esos trabajos.
- El archivado de `data/studies` usa la lista de estudios registrada en PostgreSQL para evitar respaldar carpetas huérfanas de uploads interrumpidos.
- El backup también se cancela si falta la carpeta local de un estudio activo; solo tolera carpetas ausentes de estudios ya borrados.
- Los ficheros de backup se crean con permisos privados mediante `umask 077`.

La retención de estudios se aplica de forma explícita desde el contenedor `api`:

- `make retention-dry-run DAYS=90` muestra candidatos sin borrar datos.
- `make retention-execute DAYS=90` borra estudios terminales antiguos, registra auditoría y elimina la carpeta física solo si `RETENTION_ENABLED=true`.
- Solo son candidatos los estudios `completed`, `failed` o `canceled` con antigüedad mayor o igual a `DAYS`.
- Se excluyen estudios `queued`, `processing`, ya borrados y estudios marcados con `keep_forever`.
- Mantener `RETENTION_ENABLED=false` salvo durante una ventana operativa controlada, ejecutar primero el dry-run y revisar candidatos antes de usar `retention-execute`.
- `backups/` queda ignorado por Git; debe copiarse fuera del servidor si se necesita protección real ante pérdida de disco.
- `make restore BACKUP_DIR=backups/<timestamp> CONFIRM_RESTORE=YES_I_UNDERSTAND` detiene `api` y `worker`, restaura PostgreSQL y `data/studies`, vuelve a levantar `api` y `worker`, espera a que `api` responda internamente y reinicia `reverse-proxy` para refrescar el upstream `api`.
- El restore limpia Redis con `FLUSHALL` porque la cola Celery no forma parte de la unidad restaurable.
- La restauración de PostgreSQL se ejecuta dentro de una transacción para evitar dejar la base parcialmente reemplazada si falla el SQL.
- Antes de reemplazar `data/studies`, el restore mueve la carpeta previa a `data/pre-restore/studies-<timestamp>`.
- Tras un restore, el script espera brevemente al proxy y ejecuta `scripts/smoke.sh` con reintentos cortos para confirmar que la API responde vía proxy.

El backup útil debe tratar base de datos y filesystem como una unidad lógica. Restaurar solo PostgreSQL o solo `data/studies` puede dejar estudios sin ficheros, ficheros sin metadatos o auditoría incompleta.

Redis no se respalda en esta fase: la cola Celery se considera estado transitorio. Antes de restaurar, revisar que no haya procesamientos críticos en curso.

## Dashboard Admin

La GUI muestra un dashboard operativo solo para `admin` con:

- estado de cola y jobs fallidos.
- capacidad de procesamiento simultáneo detectada desde Celery.
- healthchecks de PostgreSQL, Redis y Worker.
- uso de disco y bytes registrados en estudios.
- usuarios y estudios por estado.
- alertas básicas que no bloquean el procesamiento.

Si Redis o Worker aparecen en `warning` o `down`, revisar `docker compose ps` y `make logs` antes de relanzar trabajos.

En la GUI, cuando están disponibles, `Cancelar` y `Borrar` son acciones distintas. Un estudio en cola o en procesamiento debe cancelarse primero para conservar trazabilidad del intento; el borrado elimina después el estudio y sus ficheros. Por eso la acción `Borrar` no se muestra mientras el estudio está `queued` o `processing`. El panel admin no expone cancelación ni retry para no ejecutar pipelines desde administración.

Para `compneuro`, no aumentes `MAX_CONCURRENT_PROCESSING_JOBS` por encima de `1`. Si necesitás más paralelismo, usá más réplicas con `WORKER_REPLICAS=2` en `.env` o `make up WORKER_REPLICAS=2`. El dashboard admin muestra workers activos, capacidad total y slots disponibles.

Para preparar una configuración nueva de compneuro, partir de `.env.compneuro.example`: está comentado variable por variable y separa base de datos, cola, procesador, concurrencia, artefactos, API y SMTP. No pegar credenciales reales en archivos versionados.

## Mantenimiento

- Se usa versionado semántico: `MAJOR.MINOR.PATCH`.
- Actualizar `CHANGELOG.md` en cada release.
- Toda modificación de modelo persistente requiere migración Alembic.
- Si cambia el procesador, actualizar variables en `.env` y reiniciar `api` y `worker`.

Retención futura recomendada:

- Política inicial de 90 días para estudios no protegidos.
- Flag `keep_forever` para excluir estudios concretos.
- Modo dry-run antes de borrar físicamente.
- Auditoría de cada borrado automático o manual.
- Alerta de uso de disco en dashboard admin antes de implementar retención automática y políticas avanzadas de almacenamiento.

El borrado recomendado combina soft delete en base de datos con borrado físico controlado de input, BIDS, output, PNG, PDF, ZIP y logs, conservando una auditoría mínima.

## Logs

- Logs de contenedores: `docker compose logs`.
- Logs técnicos del procesador: `data/studies/{study_id}/logs/processor.log`.
- PNG renderizados: `data/studies/{study_id}/output/rendered_png/`.
- PDF técnico: `data/studies/{study_id}/output/reports/technical_report.pdf`.
- ZIP de resultados: `data/studies/{study_id}/output/outputs.zip`.
- Outputs compneuro: `data/studies/{study_id}/output/Preproc`.
- Log de renderizado: `data/studies/{study_id}/logs/rendering.log`.

## Incidencias

Primero revisar estado en GUI, luego logs del worker, luego `processor.log`. Para `compneuro`, comprobar además que existan `output/Preproc/BET`, `output/Preproc/ProbTissue`, `output/rendered_png/` y `logs/rendering.log`.

## Retry Y Rerun

El retry de estudios fallidos está expuesto en la GUI y crea un nuevo `ProcessingJob`, conservando trazabilidad del intento anterior.

Si se reejecuta manualmente fuera de la GUI, conservar resultados previos en una carpeta versionada o con marca temporal antes de volver a lanzar la tarea.

## Operación Futura De Jobs

- La cancelación actual cubre jobs en cola y procesamientos en ejecución. En ejecución, la API solicita terminación de la tarea Celery con `SIGTERM` y el worker/adaptador intentan terminar el grupo de procesos del comando externo.
- El retry de jobs fallidos crea un nuevo `ProcessingJob` y conserva trazabilidad del intento previo.
- Los logs visibles en GUI se devuelven truncados desde `processor.log` y `rendering.log`.
- El borrado aplica soft delete en DB y borrado físico de la carpeta del estudio; conserva auditoría mínima.
- El dashboard admin ya muestra cola, jobs activos/fallidos, uso de disco, healthchecks, worker, Redis, PostgreSQL, usuarios y estudios por estado.
