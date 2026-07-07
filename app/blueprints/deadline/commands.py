import click
from flask.cli import with_appcontext

from app.blueprints.deadline import bp


@bp.cli.command("notify")
@with_appcontext
def notify_command():
    """Send deadline notifications immediately."""
    from app.services.deadlines.deadline_notifications import send_all_deadline_notifications

    click.echo("Sending deadline notifications...")
    try:
        sent, failed = send_all_deadline_notifications()
        click.echo(f"Done. Sent: {sent}, Failed: {failed}")
    except Exception as e:
        click.echo(f"Error: {e}")


@bp.cli.command("audit-passive-status-red")
@click.option("--sample-limit", default=20, show_default=True, type=int)
@with_appcontext
def audit_passive_status_red_command(sample_limit: int):
    """Audit passive status-red docket/workflow artifacts."""
    import json

    from app.services.maintenance.passive_status_red_cleanup import passive_status_red_audit_summary

    click.echo(
        json.dumps(
            passive_status_red_audit_summary(sample_limit=sample_limit),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


@bp.cli.command("cleanup-passive-status-red")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Apply cleanup. Without this flag the command is a dry-run audit.",
)
@with_appcontext
def cleanup_passive_status_red_command(apply_changes: bool):
    """Delete auto artifacts for passive status-red rows."""
    import json

    from app.services.maintenance.passive_status_red_cleanup import (
        cleanup_passive_status_red_artifacts,
    )

    summary = cleanup_passive_status_red_artifacts(apply=apply_changes)
    if apply_changes:
        from app.extensions import db

        db.session.commit()

    click.echo(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
