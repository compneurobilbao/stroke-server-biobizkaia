# Manual De Desarrollo

## Entorno Local

```bash
cp .env.example .env
make up
make test
```

El significado de cada variable de `.env` está documentado en `docs/configuration.md`.

## Backend

- FastAPI en `backend/app`.
- Modelos en `backend/app/models`.
- Esquemas Pydantic en `backend/app/schemas`.
- Migraciones Alembic en `backend/alembic`.
- El endpoint `GET /api/admin/dashboard` agrega estado operativo para admin sin tocar el flujo de procesamiento.

## Worker

- Celery en `worker/`.
- No añadir análisis de imagen ni interpretación médica al worker; usar `processor_adapter`.
- `worker/Dockerfile.compneuro` construye el worker real sobre la imagen de `compneuro-anatproc` y añade dependencias de la plataforma.
- El post-procesado técnico corre en el mismo worker: renderiza NIfTI con FSL `slicer`, genera PDF y empaqueta resultados.
- La concurrencia recomendada para `compneuro` es `1`, porque el flujo usa `/project` como ruta fija.

## Processor Adapter

- `processor_adapter.adapter.DummyProcessorAdapter`: mantiene el procesador de desarrollo con PDF.
- `processor_adapter.adapter.CompneuroAnatprocAdapter`: prepara `/project`, ejecuta `src/apreproc_launcher.sh` y valida `Preproc/BET` y `Preproc/ProbTissue`.
- `processor_adapter.nifti_renderer`: encuentra `.nii`/`.nii.gz` en `Preproc` y ejecuta `slicer input -a output.png`.
- `processor_adapter.technical_pdf_report`: genera el PDF técnico con metadatos, listado de resultados y PNG renderizados.
- `processor_adapter.output_packager`: genera el ZIP de resultados con rutas relativas.
- `processor_adapter.artifacts`: puente de compatibilidad para imports existentes.

## BIDS

- `backend/app/services/bids.py` prepara BIDS para un único T1w por estudio.
- No se usan sesiones BIDS en esta fase.
- `bids_subject_id` debe tener formato `sub-XXXX`; ejemplo: `sub-O01`.

## Frontend

- React/Vite en `frontend/`.
- UI sencilla, castellano, clara y sin flujos de revisión médica no implementados.
- Instalar dependencias con `npm install` desde `frontend/`; el `package-lock.json` fija las versiones resueltas para reproducibilidad.
- Validar cambios frontend con `npm run lint`; la configuración vive en `frontend/eslint.config.js` y usa el formato flat config de ESLint 9.
- Si se despliega con Docker/Nginx y cambia el frontend, reconstruir el contenedor con `make frontend-rebuild`.

## Convenciones

- Conventional Commits.
- Actualizar tests ante cambios funcionales.
- Actualizar docs si cambia arquitectura, API, despliegue o flujo de procesamiento.
- Documentar cambios de tooling frontend cuando afecten instalación, lint o estructura de componentes.

## Multiusuario Básico

La plataforma incluye login local con JWT, roles `admin`/`researcher`, propietario por estudio e historial filtrado por permisos.

Puntos principales:

- `backend/app/models/user.py`: modelo `User`, roles y usuario `system`.
- `backend/app/services/auth.py`: hash PBKDF2, JWT, dependencias de usuario actual y admin.
- `backend/app/cli/create_admin.py`: creación o actualización del admin inicial.
- `backend/app/cli/retention.py`: dry-run y ejecución explícita de retención de estudios.
- `backend/alembic/versions/0004_multiuser_auth.py`: tabla `users`, `owner_user_id` y `actor_user_id`.
- `frontend/src/main.jsx`: login, sesión, “Mis estudios” y gestión básica de usuarios admin.

Crear el primer admin:

```bash
make create-admin EMAIL=admin@example.org
```

La gestión básica de trabajos incluye logs truncados, cancelación de trabajos en cola o en ejecución, reintento de fallidos y borrado seguro. El dashboard admin ya muestra estado global de cola, trabajos, almacenamiento, servicios, usuarios y estudios por estado. Backup y restore local, compartición segura de informes PDF, notificaciones, cuotas por usuario, revisión técnica de resultados y retención inicial por CLI ya están disponibles. La siguiente fase recomendada es subida múltiple y lotes.

Permisos mínimos esperados:

- `admin`: puede ver todos los estudios, gestionar usuarios y acceder a vista administrativa.
- `researcher`: puede subir, listar, ver y descargar solo estudios propios.
- Usuario no autenticado: no puede acceder a endpoints sensibles.

Fuera de la implementación actual:

- Google/OIDC.
- ORCID.
- Rol `viewer` completo.
- Compartición de ZIP mediante enlaces.
- Correo electrónico institucional avanzado.
- Retención automática.
- Subida múltiple.
- 2FA.
- Revisión clínica formal con revisor, fecha, comentarios y criterios externos validados.

## Cómo Hacer Cambios

Usa esta tabla antes de modificar el sistema. La idea es cambiar lo mínimo correcto y dejar código, tests y docs alineados.

| Cambio | Revisar | Validar |
| --- | --- | --- |
| Endpoint API, permisos o respuesta JSON | `backend/app/api/routes.py`, `backend/app/schemas/`, `docs/api.md`, tests en `tests/test_api.py` | `make test`, `make lint` |
| Modelo persistente | `backend/app/models/`, migración Alembic, `docs/architecture.md`, tests | `make test`, `make lint` |
| Worker o estados de procesamiento | `worker/tasks.py`, `processor_adapter/`, `docs/processing-pipeline.md`, `docs/operations-manual.md` | `make test`, `make lint` |
| Procesador externo o Dockerfile del worker | `processor_adapter/`, `worker/Dockerfile.compneuro`, `docs/configuration.md`, `docs/deployment.md` | `make test`, `make lint`, prueba manual si usa `compneuro` real |
| Frontend | `frontend/src/main.jsx`, `frontend/src/styles.css`, `frontend/README.md`, manual de usuario si cambia el flujo | `npm run lint` desde `frontend/` |
| Configuración o variables `.env` | `.env.example`, `docker-compose.yml`, `backend/app/core/config.py`, `docs/configuration.md` | `make check-secrets`, `make check-docs` |
| Documentación pura | README/doc afectado y enlaces cruzados | `make check-docs` |

No leas ni pegues `.env` en conversaciones o commits. No uses datos reales identificativos en fixtures, ejemplos o capturas.
