# Infra

This folder contains the project's local infrastructure configuration.

## Responsibility

- Configure the reverse proxy.
- Keep infrastructure separate from application code.
- Document reproducible deployment decisions with Docker Compose.

## Code Layout

- `reverse-proxy/nginx.conf`: Nginx configuration used by the `reverse-proxy` service.

## Proxy Flow

- `/api/` routes are forwarded to the `api:8000` service.
- All other routes are forwarded to the `frontend:80` service.
- `client_max_body_size` allows the large uploads configured for neuroimaging studies.

## Future

A hospital phase would need real TLS, security headers, finer limits, controlled access logs and hardening.
