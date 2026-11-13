# Scripts

Esta carpeta contiene scripts pequeños de operación y validación.

## Responsabilidad

- Facilitar comandos repetitivos usados por `Makefile`.
- Evitar instrucciones manuales propensas a error.
- Preparar checks básicos para desarrollo y pre-commit.

## Scripts

- `start.sh`: levanta servicios con `docker compose up -d`.
- `stop.sh`: detiene servicios con `docker compose down`.
- `smoke.sh`: comprueba el healthcheck vía reverse proxy.
- `check-docs.sh`: valida que exista la documentación mínima obligatoria.
- `check-no-secrets.sh`: bloquea ficheros `.env*` commiteados (salvo plantillas `*.example`), datos de estudios y señales simples de secretos; ejecuta `gitleaks` si está instalado.
- `backup.sh`: crea un backup local de PostgreSQL y `data/studies`; pausa `api`/`worker`, rechaza jobs activos y archiva solo estudios registrados en DB.
- `restore.sh`: restaura PostgreSQL y `data/studies` desde un backup local con confirmación explícita, limpiando Redis como estado transitorio.
- `seed.sh`: crea un fichero dummy local para pruebas manuales.

## Relación Con Makefile

El `Makefile` llama a estos scripts mediante comandos como `make smoke`, `make backup`, `make restore`, `make check-docs`, `make check-secrets` y `make seed`.

## Criterio

Los scripts deben seguir siendo simples, legibles y seguros. No deben borrar datos reales salvo que el comando lo indique claramente. El restore debe conservar una copia previa local antes de reemplazar `data/studies`.
