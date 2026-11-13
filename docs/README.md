# Docs

Esta carpeta contiene la documentación técnica, operativa y de defensa del TFM.

## Responsabilidad

- Explicar arquitectura, despliegue, operación, seguridad y mantenimiento.
- Documentar el pipeline de procesamiento.
- Mantener un roadmap realista.
- Servir como apoyo para la memoria y defensa del TFM.

## Documentos Principales

- `plan/IMPLEMENTATION_PLAN.md`: plan de implementación de la plataforma y los pipelines (en inglés).
- `adr/`: registros de decisiones de arquitectura (en inglés).
- `architecture.md`: arquitectura, componentes, ER y estados.
- `processing-pipeline.md`: flujo API, Redis, worker, adapter, script y PDF.
- `roadmap.md`: estado actual y roadmap de evolución funcional por fases.
- `configuration.md`: variables de entorno y uso de `.env`.
- `deployment.md`: instalación local y despliegue con Docker Compose.
- `user-manual.md`: manual para usuario funcional.
- `developer-manual.md`: guía para desarrollo y cambios seguros por tipo de modificación.
- `operations-manual.md`: operación diaria, administración básica y mantenimiento.
- `security.md`: seguridad actual y pendientes.
- `api.md`: endpoints principales.
- `testing.md`: pruebas disponibles.
- `ai-development-rules.md`: reglas para desarrollo asistido por IA.
- `troubleshooting.md`: incidencias habituales.
- `demo-guide.md`: guion operativo para demostrar la plataforma ante tribunal.
- `tfm-technical-summary.md`: resumen técnico para el TFM.
- `agentic-workflow-audit.md`: auditoría y decisiones de configuración OpenCode/agentes.

## Regla De Mantenimiento

Si cambia arquitectura, endpoints, despliegue, flujo de procesamiento o seguridad, hay que actualizar esta carpeta junto con el código.
