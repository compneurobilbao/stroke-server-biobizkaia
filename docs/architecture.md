# Arquitectura

El sistema usa una arquitectura desacoplada para evitar que la API web dependa del algoritmo concreto de procesamiento. El procesador externo se invoca mediante `processor_adapter` como componente aislado y puede seleccionarse por configuración (`dummy` o `compneuro`). Al cerrar un procesamiento, el worker registra notificaciones internas y opcionalmente envía correos electrónicos por SMTP sin adjuntar resultados.

```mermaid
flowchart TB
  subgraph Client
    Browser[GUI web castellano]
  end
  subgraph DockerHost[Servidor unico Docker Compose]
    Proxy[Nginx reverse-proxy]
    Frontend[frontend React]
    API[api FastAPI]
    DB[(PostgreSQL)]
    Redis[(Redis broker)]
    Worker[Celery worker]
    Adapter[processor_adapter]
    Script[procesador externo]
    Render[Render NIfTI a PNG con FSL slicer]
    Artifacts[PDF tecnico / ZIP]
    Notify[Notificaciones internas / SMTP]
    Storage[(data/studies filesystem)]
  end
  Browser --> Proxy
  Proxy --> Frontend
  Proxy --> API
  API --> DB
  API --> Storage
  API --> Redis
  Redis --> Worker
  Worker --> DB
  Worker --> Storage
  Worker --> Adapter
  Adapter --> Script
  Script --> Storage
  Worker --> Render
  Render --> Artifacts
  Artifacts --> Storage
  Worker --> Notify
  Notify --> DB
```

Para `compneuro-anatproc`, el servicio `worker` puede construirse con `worker/Dockerfile.compneuro`, derivado de `compneurobilbaolab/compneuro-anatproc:1.1`. No se usa Docker-in-Docker: Celery, el launcher externo y FSL `slicer` conviven en el mismo contenedor worker.

No existe un contenedor `compneuro-anatproc` anidado dentro del `worker`. El `worker` es una imagen derivada de `compneurobilbaolab/compneuro-anatproc:1.1`; por eso ejecuta directamente los scripts y herramientas de neuroimagen dentro del mismo contenedor.

Esta decisión no obliga a usar siempre esa imagen ni ese launcher. La frontera mantenible es `processor_adapter`: un futuro procesador puede usar otro script o un worker construido desde otra imagen si respeta la interfaz de entrada y salida esperada, y deja a la plataforma la trazabilidad, el PDF técnico y el ZIP descargable.

## Componentes

- `frontend`: interfaz simple para subida, listado, estado y descarga.
- `api`: valida entradas, registra estudios, prepara BIDS, expone OpenAPI, descarga PDF/ZIP y sirve notificaciones/preferencias.
- `worker`: ejecuta tareas largas fuera del ciclo HTTP y dispara notificaciones al finalizar.
- `processor_adapter`: interfaz estable con el procesador externo y estrategia por backend.
- `postgres`: persistencia relacional.
- `redis`: cola de tareas.
- `filesystem`: almacenamiento inicial sustituible por S3/MinIO futuro.
- `reverse-proxy`: punto de entrada HTTP.

## Modelo ER

El modelo siguiente refleja el estado implementado actualmente, incluyendo autenticación local, propietario por estudio, enlaces temporales de compartición PDF y notificaciones.

```mermaid
erDiagram
  User ||--o{ Study : owns
  User ||--o{ AuditEvent : performs
  User ||--o{ ShareLink : creates
  User ||--o{ Notification : receives
  Study ||--o{ ProcessingJob : has
  Study ||--o{ AuditEvent : emits
  Study ||--o{ ShareLink : shares
  Study ||--o{ Notification : notifies
  User {
    uuid id PK
    string email
    string full_name
    text hashed_password
    string role
    bool is_active
    datetime deleted_at
    bigint storage_quota_bytes
    bool notify_on_processing_completed
    bool notify_on_processing_failed
    datetime created_at
    datetime updated_at
    datetime last_login_at
  }
  Study {
    uuid id PK
    uuid owner_user_id FK
    string original_filename
    text stored_path
    text output_path
    text pdf_path
    text output_zip_path
    string bids_subject_id
    string processor_backend
    string container_image
    text bids_path
    text preproc_output_path
    text rendered_png_dir
    text processing_warnings
    string clinical_review_status
    enum status
    datetime created_at
    datetime updated_at
    datetime processing_started_at
    datetime processing_finished_at
    datetime deleted_at
    text error_message
    string processor_name
    string processor_version
    bigint file_size
    string checksum
  }
  ProcessingJob {
    uuid id PK
    uuid study_id FK
    string celery_task_id
    string status
    datetime queued_at
    datetime started_at
    datetime finished_at
    int retry_count
    string worker_name
    int exit_code
    text log_path
    text error_message
  }
  AuditEvent {
    uuid id PK
    uuid study_id FK
    uuid actor_user_id FK
    string event_type
    datetime timestamp
    text details
    string actor
    string ip_address
  }
  ShareLink {
    uuid id PK
    uuid study_id FK
    uuid created_by_user_id FK
    string token_hash
    datetime expires_at
    datetime revoked_at
    datetime created_at
    datetime last_accessed_at
    int access_count
  }
  Notification {
    uuid id PK
    uuid recipient_user_id FK
    uuid study_id FK
    string event_type
    string title
    text message
    datetime created_at
    datetime read_at
    string email_status
    datetime email_sent_at
    text email_error
  }
```

## Arquitectura Multiusuario

La API aplica autenticación y autorización antes de operar sobre estudios. El worker no conoce sesiones ni roles: recibe IDs de estudios ya validados por la API y conserva la frontera con `processor_adapter`.

Reglas implementadas:

- `admin` puede ver todos los estudios, crear usuarios, borrar usuarios lógicamente y definir cuotas de almacenamiento por usuario.
- `admin` puede consultar un dashboard administrativo centrado en usuarios, estudios y almacenamiento; el panel no expone acciones para ejecutar pipelines.
- `admin` puede marcar salidas como `technical_only`, `reviewed` o `validated` en cualquier estudio visible. El valor inicial es `technical_only` porque el PDF no es un informe clínico.
- `researcher` puede subir estudios, ver historial propio, consultar su cuota, descargar resultados propios y marcar sus salidas como `technical_only`, `reviewed` o `validated`.
- `admin` y propietario pueden crear/revocar enlaces temporales para compartir solo el PDF técnico de estudios completados.
- Los receptores externos descargan por token opaco sin cuenta, sin acceso a ZIP, logs, detalle ni enumeración de estudios.
- El propietario recibe notificaciones internas al completar o fallar un procesamiento; los admins activos reciben aviso interno ante fallos.
- Los correos electrónicos usan Mailpit local por defecto en Docker Compose (`noreply@neuroimagen.com`) o un SMTP real por configuración; nunca adjuntan PDF, ZIP ni logs.
- Los usuarios iniciales se crean por admin; no hay registro público abierto.
- El usuario admin inicial se crea con `make create-admin EMAIL=...`.
- El flujo de procesamiento sigue aislado detrás de `processor_adapter`; autenticación y permisos pertenecen a la capa API.

Evolución posterior:

- Un rol `viewer` completo no entra en la implementación actual. La compartición actual se limita a PDF técnico mediante token temporal opaco, caducidad, revocación y auditoría.
- Google/OIDC y ORCID deben vincularse a usuarios internos existentes o aprovisionados, sin sustituir el modelo de permisos propio.
- Backups, restore local y retención inicial se cubren como operaciones CLI; una pantalla admin avanzada de retención queda para una fase posterior.

## Estados

```mermaid
stateDiagram-v2
  [*] --> uploaded
  uploaded --> queued
  queued --> processing
  processing --> completed
  processing --> failed
  processing --> canceled
  completed --> [*]
  failed --> [*]
```

La revisión clínica no cambia el estado de procesamiento: se registra aparte con `technical_only`, `reviewed` o `validated` para no presentar el PDF técnico como informe clínico.

La cancelación implementada usa el estado `canceled` para trabajos en cola y en procesamiento. En cola se revoca la tarea pendiente; durante procesamiento la API pide a Celery terminar la tarea y el adaptador ejecuta el procesador externo en un grupo de procesos propio para propagar la señal a scripts/hijos de FSL o `compneuro`.

No se añade un estado `bids_prepared` en la primera integración: la preparación BIDS ocurre antes de encolar y queda trazada mediante campos y auditoría. Si falla, la subida responde con error y no crea un estudio procesable.

## Worker Compneuro

```mermaid
flowchart LR
  API[FastAPI] --> Data[(data/studies)]
  API --> Redis[(Redis)]
  Redis --> Worker[Celery worker compneuro]
  Worker --> Adapter[CompneuroAnatprocAdapter]
  Adapter --> Project[/project symlink gestionado]
  Project --> BIDS[bids_project/data]
  Project --> Preproc[output/Preproc]
  Adapter --> Launcher[src/apreproc_launcher.sh]
  Launcher --> Preproc
  Worker --> PNG[output/rendered_png]
  PNG --> PDF[output/reports/technical_report.pdf]
  Worker --> ZIP[output/outputs.zip]
```

No se mantiene una copia local de `compneuro-anatproc/` como dependencia del proyecto. El worker real parte de la imagen Docker publicada y, durante el build, copia únicamente los scripts versionados necesarios para ejecutar `src/apreproc_launcher.sh`.

Para reemplazar el procesador, no hace falta modificar FastAPI ni la GUI si se mantiene la interfaz esperada. El punto de extensión previsto es crear o ajustar un adapter, cambiar `PROCESSOR_BACKEND`/comando, y construir un worker que incluya las herramientas necesarias.

## Decisiones Arquitectónicas

- La API no ejecuta procesamiento de neuroimagen; valida, prepara datos, registra el estudio y encola la tarea.
- Celery ejecuta el procesamiento largo fuera del ciclo HTTP para evitar bloqueos y permitir trazabilidad de estados.
- `processor_adapter` mantiene al procesador externo como componente aislado y evita acoplar FastAPI al flujo de procesamiento de neuroimagen concreto.
- El post-procesado técnico se ejecuta en el mismo worker porque FSL ya está disponible y se evitan contenedores, volúmenes y sincronización adicionales.
- La concurrencia segura con `compneuro` se consigue escalando réplicas del servicio `worker`; cada contenedor mantiene `MAX_CONCURRENT_PROCESSING_JOBS=1` para no compartir su `/project` interno entre procesos simultáneos.
- El PDF generado es técnico y no contiene interpretación de imagen ni conclusiones médicas.
