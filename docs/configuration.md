# Configuración

La configuración local se define copiando `.env.example` a `.env` y ajustando sus valores antes de levantar los servicios.

```bash
cp .env.example .env
```

El fichero `.env` no debe commitearse. Puede contener contraseñas, rutas locales o comandos específicos del entorno.

## Variables Principales

| Variable | Uso | Valor por defecto |
| --- | --- | --- |
| `ENVIRONMENT` | Nombre del entorno de ejecución. | `development` |
| `POSTGRES_DB` | Base de datos creada por el contenedor PostgreSQL. | `neuroimagen` |
| `POSTGRES_USER` | Usuario de PostgreSQL. | `neuroimagen` |
| `POSTGRES_PASSWORD` | Contraseña de PostgreSQL. Debe cambiarse fuera de desarrollo. | `neuroimagen` |
| `DATABASE_URL` | URL usada por API y worker para conectarse a PostgreSQL. | `postgresql+psycopg://neuroimagen:neuroimagen@postgres:5432/neuroimagen` |
| `CELERY_BROKER_URL` | Broker Redis usado por Celery para encolar tareas. | `redis://redis:6379/0` |
| `CELERY_RESULT_BACKEND` | Backend Redis usado por Celery para resultados técnicos. | `redis://redis:6379/1` |
| `STORAGE_ROOT` | Ruta interna donde se guardan estudios, resultados y logs. | `/app/data/studies` |
| `ALLOWED_EXTENSIONS` | Extensiones aceptadas en la subida de estudios. | `.nii,.nii.gz,.dcm,.zip,.tar,.tar.gz,.gz,.json,.txt` |
| `MAX_UPLOAD_SIZE_MB` | Tamaño máximo de fichero permitido, en MB. | `1024` |
| `PROCESSOR_NAME` | Nombre técnico del procesador configurado. | `dummy-development-processor` |
| `PROCESSOR_VERSION` | Versión técnica del procesador configurado. | `0.1.0` |
| `PROCESSOR_BACKEND` | Backend activo: `dummy` o `compneuro`. | `dummy` |
| `PROCESSOR_COMMAND` | Plantilla de comando usada por el backend `dummy`. | Dummy de desarrollo |
| `WORKER_DOCKERFILE` | Dockerfile usado para construir el worker. | `backend/Dockerfile` |
| `WORKER_REPLICAS` | Réplicas del servicio `worker` usadas por `make up`/`make rebuild`. Puede definirse en `.env` o pasarse al comando. | `1` |
| `COMPNEURO_CONTAINER_IMAGE` | Imagen base documentada para trazabilidad de compneuro. | `compneurobilbaolab/compneuro-anatproc:1.1` |
| `COMPNEURO_ANATPROC_REF` | Commit del repo externo usado al construir el worker compneuro. | `a2f3e7c9523ed521c3f85f7dffde5ee8fb400842` |
| `COMPNEURO_PROJECT_MOUNT` | Ruta que el procesador externo ve como proyecto. | `/project` |
| `COMPNEURO_COMMAND` | Comando ejecutado por el adapter compneuro. | `bash /app/src/apreproc_launcher.sh` |
| `PROCESSING_TIMEOUT_SECONDS` | Timeout del procesador; `0` lo desactiva. | `0` |
| `MAX_CONCURRENT_PROCESSING_JOBS` | Concurrencia interna de cada worker. Para compneuro debe mantenerse en `1`. | `1` |
| `GENERATE_OUTPUT_ZIP` | Generar ZIP de resultados `Preproc`. | `true` |
| `GENERATE_TECHNICAL_PDF` | Generar PDF técnico de procesamiento. | `true` |
| `GENERATE_RENDERED_PNG` | Renderizar resultados NIfTI a PNG tras `compneuro`. | `true` |
| `NIFTI_RENDERER` | Herramienta CLI de renderizado. En compneuro debe ser FSL `slicer`. | `slicer` |
| `NIFTI_RENDER_MAX_FILES` | Máximo de NIfTI a renderizar por estudio. | `50` |
| `NIFTI_RENDER_TIMEOUT_SECONDS` | Timeout por fichero renderizado. | `300` |
| `TECHNICAL_REPORT_FILENAME` | Nombre del PDF dentro de `output/reports/`. | `technical_report.pdf` |
| `BIDS_VALIDATE` | Reservado para validación BIDS formal futura. | `false` |
| `CORS_ORIGINS` | Orígenes permitidos para llamadas desde navegador. | `http://localhost,http://localhost:5173` |
| `AUTH_SECRET_KEY` | Clave HMAC para firmar tokens JWT. Debe cambiarse fuera de desarrollo. | `change-me-in-production` |
| `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` | Duración del access token local. | `480` |
| `SHARE_LINK_EXPIRE_HOURS` | Caducidad por defecto de enlaces temporales de descarga PDF. | `72` |
| `RETENTION_ENABLED` | Permite la ejecución real de borrado por retención. El dry-run no requiere activación. | `false` |
| `RETENTION_DAYS` | Días mínimos de conservación usados por el CLI de retención. | `90` |
| `APP_PUBLIC_BASE_URL` | URL pública incluida en los correos de notificación. | `http://100.114.173.115` |
| `NOTIFICATIONS_EMAIL_ENABLED` | Activa el intento de envío SMTP además de notificaciones internas. | `true` en Docker Compose |
| `SMTP_HOST` | Servidor SMTP usado por el worker para enviar correos electrónicos. | `mailpit` |
| `SMTP_PORT` | Puerto SMTP. | `1025` |
| `SMTP_USERNAME` | Usuario SMTP si el relay requiere autenticación. | vacío |
| `SMTP_PASSWORD` | Contraseña o API key SMTP. No commitear. | vacío |
| `SMTP_FROM_EMAIL` | Remitente autorizado por el servidor SMTP. | `noreply@neuroimagen.com` |
| `SMTP_USE_TLS` | Usa STARTTLS antes de autenticar. | `false` con Mailpit |

## Usuario Admin Inicial

No hay registro público abierto. Tras aplicar migraciones, crear o actualizar el primer admin con:

```bash
make create-admin EMAIL=admin@example.org
```

El comando solicita la contraseña por consola y ejecuta `python -m app.cli.create_admin` dentro del contenedor `api`.

La autenticación local usa JWT firmado con `AUTH_SECRET_KEY`. En desarrollo existe un valor por defecto, pero en cualquier entorno compartido debe configurarse una clave propia y no commitearla. La API rechaza arrancar fuera de `development` si la clave sigue siendo el valor por defecto o es demasiado corta.

Los enlaces de compartición usan tokens aleatorios opacos y solo guardan su hash en base de datos. `SHARE_LINK_EXPIRE_HOURS` define la caducidad por defecto cuando el usuario crea un enlace desde la GUI o la API.

La política de retención se configura con `RETENTION_DAYS` y se aplica por CLI. La ejecución real está desactivada por defecto con `RETENTION_ENABLED=false`; el comando sin `--execute` funciona como dry-run y permite revisar candidatos sin borrar datos:

```bash
make retention-dry-run DAYS=90
make retention-execute DAYS=90
```

`make retention-execute` solo borra si `RETENTION_ENABLED=true` está configurado en el entorno del contenedor `api`.

La retención solo afecta a estudios terminales (`completed`, `failed`, `canceled`) y excluye `queued`, `processing`, estudios ya borrados y estudios con `keep_forever`.

## Notificaciones Por SMTP

En desarrollo Docker Compose usa Mailpit por defecto. Mailpit captura correos en `http://localhost:8025`, pero no entrega mensajes a Internet. Para enviar a Gmail u otros buzones reales hay que usar un relay SMTP autenticado o un dominio con SPF/DKIM correctos.

Los ficheros `.env.example` y `.env.compneuro.example` incluyen la configuración SMTP de Brevo preparada para este despliegue, con `SMTP_PASSWORD` vacío. La clave SMTP real debe añadirse solo en `.env` local y nunca debe commitearse.

Configuración ejemplo con Brevo:

```env
NOTIFICATIONS_EMAIL_ENABLED=true
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USERNAME=usuario-smtp-de-brevo
SMTP_PASSWORD=clave-smtp-generada-en-brevo
SMTP_FROM_EMAIL=remitente-verificado-en-brevo
SMTP_USE_TLS=true
```

`SMTP_FROM_EMAIL` debe ser un remitente verificado en Brevo o pertenecer a un dominio verificado allí. No uses `noreply@neuroimagen.com` salvo que el dominio esté autorizado con SPF/DKIM en el proveedor.

El envío lo ejecuta el servicio `worker` cuando cierra un procesamiento. Si se cambia la configuración SMTP, reiniciar `api` y `worker`; reiniciar solo la API no actualiza el proceso que envía los correos.

## `PROCESSOR_COMMAND`

`PROCESSOR_COMMAND` es la plantilla de comando para el procesador de desarrollo (`PROCESSOR_BACKEND=dummy`). No hay que acoplar la API ni el worker al script externo: el worker siempre invoca `processor_adapter`, y el adaptador decide qué comando ejecutar según el backend activo.

En modo `dummy`, el adaptador ejecuta `PROCESSOR_COMMAND`. En modo `compneuro`, el adaptador no usa `PROCESSOR_COMMAND` como comando principal, sino `COMPNEURO_COMMAND`.

Ejemplo:

```env
PROCESSOR_COMMAND=python /app/external_processor/dummy_processor.py --input {input_dir} --output {output_dir} --study-id {study_id}
```

Placeholders disponibles:

- `{input_dir}`: directorio con el fichero subido.
- `{output_dir}`: directorio donde el script debe generar el PDF.
- `{study_id}`: identificador UUID del estudio.
- `{logs_dir}`: directorio reservado para logs técnicos.

En modo `dummy`, el script configurado debe generar al menos un fichero `.pdf` dentro de `output_dir`. Si no lo hace, el procesamiento se marca como fallido.

## `PROCESSOR_BACKEND=compneuro`

La integración real ejecuta `compneuro-anatproc` dentro del worker, no mediante Docker-in-Docker. Configuración mínima:

```env
PROCESSOR_BACKEND=compneuro
PROCESSOR_NAME=compneuro-anatproc
PROCESSOR_VERSION=1.1
WORKER_DOCKERFILE=worker/Dockerfile.compneuro
ALLOWED_EXTENSIONS=.nii.gz
WORKER_REPLICAS=2
MAX_CONCURRENT_PROCESSING_JOBS=1
GENERATE_OUTPUT_ZIP=true
GENERATE_TECHNICAL_PDF=true
GENERATE_RENDERED_PNG=true
NIFTI_RENDERER=slicer
```

Para correr más de un procesamiento `compneuro` a la vez, escalá réplicas del servicio `worker` en lugar de subir la concurrencia interna del contenedor. Ejemplo: `WORKER_REPLICAS=2` en `.env` o `make up WORKER_REPLICAS=2` permite dos procesamientos simultáneos si hay recursos suficientes. La capacidad efectiva se muestra en el dashboard admin.

En este modo el comando principal es `COMPNEURO_COMMAND`, por defecto `bash /app/src/apreproc_launcher.sh`. El worker se construye sobre `compneurobilbaolab/compneuro-anatproc:1.1`, por lo que ejecuta el launcher y FSL `slicer` dentro del mismo contenedor worker.

## Plantilla `.env.compneuro.example`

`.env.compneuro.example` es la referencia comentada para preparar un `.env` real con `PROCESSOR_BACKEND=compneuro`. Cada variable incluye un comentario inmediato con su propósito para que la configuración sea revisable antes de levantar servicios.

Bloques principales:

- Entorno y PostgreSQL: `ENVIRONMENT`, `POSTGRES_*`, `DATABASE_URL`.
- Cola Celery/Redis: `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`.
- Almacenamiento y subida: `STORAGE_ROOT`, `ALLOWED_EXTENSIONS`, `MAX_UPLOAD_SIZE_MB`.
- Procesador compneuro: `PROCESSOR_*`, `WORKER_DOCKERFILE`, `COMPNEURO_*`.
- Concurrencia: `WORKER_REPLICAS` define cuántos contenedores worker se levantan; `MAX_CONCURRENT_PROCESSING_JOBS` define procesos internos por contenedor y debe quedar en `1` para compneuro.
- Artefactos técnicos: `GENERATE_*`, `NIFTI_*`, `TECHNICAL_REPORT_FILENAME`.
- API y seguridad: `CORS_ORIGINS`, `AUTH_*`, `APP_PUBLIC_BASE_URL`.
- Notificaciones SMTP: `NOTIFICATIONS_EMAIL_ENABLED`, `SMTP_*`.

Antes de copiarla a `.env`, revisar especialmente `POSTGRES_PASSWORD`, `AUTH_SECRET_KEY`, `APP_PUBLIC_BASE_URL`, `SMTP_USERNAME`, `SMTP_PASSWORD` y `SMTP_FROM_EMAIL`. Los ejemplos versionados no deben contener credenciales reales.

`COMPNEURO_COMMAND` es el punto configurable para cambiar de script manteniendo el backend `compneuro`. Si el nuevo procesador requiere otras dependencias, el cambio debe acompañarse con otro `WORKER_DOCKERFILE` o con una imagen base distinta. El contenedor resultante sigue siendo el servicio `worker`: debe tener Celery, las dependencias Python de la plataforma, acceso a `/app/data` y el comando configurado disponible dentro del contenedor.

Para sustituir el procesador de forma más amplia, revisar:

- `PROCESSOR_BACKEND`: backend activo que selecciona el adapter.
- `COMPNEURO_COMMAND` o el comando equivalente del nuevo backend.
- `WORKER_DOCKERFILE`: Dockerfile que construye el worker con las herramientas necesarias.
- `COMPNEURO_CONTAINER_IMAGE`/imagen base documentada, si se mantiene este backend.
- estructura de resultados esperada por `worker/tasks.py` y `processor_adapter/output_packager.py`.

La API prepara automáticamente BIDS para un único sujeto T1w. Si el sujeto se deja vacío, genera un identificador seguro. Si se informa un valor inválido, responde `400`.

La carpeta local `compneuro-anatproc/` no es necesaria para ejecutar la integración. Puede existir como referencia temporal ignorada por Git, pero el flujo soportado usa la imagen Docker y `worker/Dockerfile.compneuro`.

## Cómo Sustituir El Procesador

Para cambiar la imagen Docker o usar otro script, mantén la separación entre plataforma y procesador externo:

1. Crear o adaptar un Dockerfile de worker con Celery, las dependencias Python de la plataforma y las herramientas del nuevo procesador.
2. Configurar `WORKER_DOCKERFILE` para construir ese worker.
3. Configurar `COMPNEURO_COMMAND` si basta con cambiar el script dentro del backend `compneuro`, o crear un backend nuevo en `processor_adapter` si cambia la interfaz esperada.
4. Asegurar que el comando nuevo lea los datos preparados por la plataforma y escriba resultados en la salida esperada, idealmente `output/Preproc`.
5. Dejar que el worker genere los artefactos propios de la plataforma: `output/reports/technical_report.pdf` y `output/outputs.zip`.
6. Reconstruir y reiniciar `worker`; reiniciar `api` si cambia configuración compartida.

No hace falta modificar la GUI ni los endpoints de FastAPI si el nuevo procesador respeta la interfaz esperada descrita en `docs/processing-pipeline.md`.

## Seguridad Operativa

- No commitear `.env`.
- No poner secretos reales en `.env.example`.
- Cambiar `POSTGRES_PASSWORD` fuera de desarrollo local.
- No usar datos reales identificativos o sensibles en pruebas o ejemplos.
- Mantener `ALLOWED_EXTENSIONS` limitado a formatos esperados.
- Revisar `MAX_UPLOAD_SIZE_MB` según capacidad de disco y política operativa.
- Si cambia el comando del procesador o el Dockerfile del worker, reiniciar o reconstruir los servicios afectados.

## Relación Con Docker Compose

`docker-compose.yml` lee estas variables y aplica valores por defecto si no están definidas. En desarrollo local suele bastar con crear `.env` desde `.env.example`; en despliegues reales hay que revisar contraseñas, rutas, CORS y comando del procesador antes de levantar servicios.

`WORKER_REPLICAS` no se inyecta dentro de los contenedores. `make up` y `make rebuild` leen esa clave desde `.env` si existe, o desde el parámetro de línea de comandos si se pasa explícitamente. Ejemplo: `make up WORKER_REPLICAS=2` ejecuta `docker compose up -d --scale worker=2`.
