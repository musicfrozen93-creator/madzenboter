"""
Zentry Market Analysis API — Entry Point.

Runs the FastAPI analysis service. This process analyses markets on request and
returns trading signals; it has no trading loop, no exchange credentials, and no
order path.

Usage:
    python main.py                      # serve on 0.0.0.0:8000
    python main.py --port 9000          # custom port
    python main.py --reload             # auto-reload for development
    python main.py --check              # validate config and exit
    python main.py --init-db            # create database tables and exit

Environment variables:
    Loaded from a `.env` file in the working directory (if present), then from
    the system environment. System variables always take precedence over .env.
    See .env.example.

        DATABASE_URL      PostgreSQL connection string
        ZENTRY_CONFIG     Path to config.json (default: config/config.json)
        ALLOWED_ORIGINS   Comma-separated CORS origins for the frontend
        API_HOST/API_PORT Bind address and port (default 0.0.0.0:8000)
"""

import argparse
import os
import sys

# Load .env FIRST — before any os.environ.get() call in Settings.
# System environment variables always win (override=False is the dotenv default).
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass  # python-dotenv not installed; rely on system env vars only


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Zentry Market Analysis API — market analysis and signal generation',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Serve the API:
    python main.py

  Development with auto-reload:
    python main.py --reload

  Validate configuration without starting the server:
    python main.py --check

  Create database tables and exit:
    python main.py --init-db
        """,
    )
    parser.add_argument(
        '--host', default=os.environ.get('API_HOST', '0.0.0.0'),
        help='Bind address (default: 0.0.0.0)',
    )
    parser.add_argument(
        '--port', type=int, default=int(os.environ.get('API_PORT', 8000)),
        help='Bind port (default: 8000)',
    )
    parser.add_argument(
        '--config', default=os.environ.get('ZENTRY_CONFIG', 'config/config.json'),
        help='Path to the configuration file (default: config/config.json)',
    )
    parser.add_argument(
        '--reload', action='store_true',
        help='Auto-reload on source changes (development only)',
    )
    parser.add_argument(
        '--workers', type=int, default=int(os.environ.get('API_WORKERS', 1)),
        help='Number of worker processes (ignored with --reload)',
    )
    parser.add_argument(
        '--check', action='store_true',
        help='Validate configuration and registered providers, then exit',
    )
    parser.add_argument(
        '--init-db', action='store_true',
        help='Create database tables and exit',
    )
    return parser.parse_args()


def main() -> None:
    """Parse arguments and dispatch."""
    args = _parse_args()

    # api.dependencies reads this to locate config.json.
    os.environ['ZENTRY_CONFIG'] = args.config

    from config.settings import Settings

    try:
        settings = Settings.load(args.config)
    except FileNotFoundError:
        print(f'Error: Config file not found: {args.config}')
        sys.exit(1)
    except Exception as e:
        print(f'Error loading config: {e}')
        sys.exit(1)

    issues = settings.validate()
    if issues:
        print('Error: invalid configuration:')
        for issue in issues:
            print(f'  - {issue}')
        sys.exit(1)

    # ── Create database tables ──
    if args.init_db:
        from core.database import Database

        db = Database(settings.database_url)
        db.initialize()
        db.close()
        print('✓ Database tables created.')
        return

    # ── Configuration check ──
    if args.check:
        from providers.registry import describe_providers

        print('✓ Configuration valid.')
        print(f'  Config file : {args.config}')
        print(f'  Timeframes  : {settings.timeframe} (default)')
        print('  Providers   :')
        for provider in describe_providers():
            default = ' [default]' if provider['is_default'] else ''
            print(
                f'    - {provider["provider"]} ({provider["market"]}){default}: '
                f'{len(provider["timeframes"])} timeframes'
            )
        return

    # ── Serve ──
    import uvicorn

    print()
    print('═' * 58)
    print('  ZENTRY MARKET ANALYSIS API')
    print('  Analysis and signal generation only — trading is not possible')
    print(f'  Listening on http://{args.host}:{args.port}')
    print(f'  Docs at      http://{args.host}:{args.port}/docs')
    print('═' * 58)
    print()

    uvicorn.run(
        'api.app:app',
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=None if args.reload else args.workers,
        log_level=settings.log_level.lower(),
    )


if __name__ == '__main__':
    main()
