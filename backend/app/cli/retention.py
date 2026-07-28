from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.retention import apply_retention_policy
from app.services.storage import LocalStudyStorage


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Aplicar la política de retención de estudios"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=settings.retention_days,
        help="Días mínimos de conservación antes de considerar borrado",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Ejecuta el borrado. Sin este flag solo hace dry-run.",
    )
    args = parser.parse_args()
    if args.execute and not settings.retention_enabled:
        raise SystemExit(
            "La retención está desactivada. Configurá RETENTION_ENABLED=true "
            "para permitir borrados."
        )

    db = SessionLocal()
    try:
        result = apply_retention_policy(
            db,
            LocalStudyStorage(settings.storage_root),
            args.days,
            dry_run=not args.execute,
        )
    finally:
        db.close()

    mode = "dry-run" if result.dry_run else "execute"
    print(
        f"Retención {mode}: {len(result.candidates)} candidato(s), "
        f"{len(result.deleted)} borrado(s), cutoff={result.cutoff.isoformat()}"
    )
    for candidate in result.candidates:
        print(
            f"- {candidate.id} | {candidate.status} | "
            f"{candidate.created_at.isoformat()} | {candidate.original_filename}"
        )


if __name__ == "__main__":
    main()
