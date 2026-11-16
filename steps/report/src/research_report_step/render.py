"""Jinja2 templates to HTML and WeasyPrint to PDF, with no access to files or network."""

from __future__ import annotations

import base64
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
LANGUAGES = ("en", "es")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$")


def data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _fixed(value, digits: int = 3) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def _percent(value, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.{digits}f} %"


def _p(value) -> str:
    if value is None:
        return "-"
    value = float(value)
    return "< 0.0001" if value < 0.0001 else f"{value:.4f}"


def _short_digest(value: str | None, n: int = 12) -> str:
    return "-" if not value else value[: n + 7] if value.startswith("sha256:") else value[:n]


def environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(fixed=_fixed, percent=_percent, pval=_p, short=_short_digest)
    return env


def render_html(language: str, report: dict, figures: dict) -> str:
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language {language!r}")
    created = report["provenance"]["run"]["created_at"]
    template = environment().get_template(f"report_{language}.html.j2")
    return template.render(
        r=report,
        fig=figures,
        created_meta=created if _ISO_DATE.match(created) else None,
    )


def _fetch_data_only(url, *args, **kwargs):
    from weasyprint import default_url_fetcher

    if not url.startswith("data:"):
        raise ValueError("external resources are not allowed in the report")
    return default_url_fetcher(url)


def write_pdf(html: str, out: Path) -> None:
    from weasyprint import HTML

    HTML(string=html, url_fetcher=_fetch_data_only).write_pdf(str(out))
