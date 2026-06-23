#!/usr/bin/env python3
"""
ZenGrid — Generate a Fernet Encryption Key.

CLI utility that creates a new, cryptographically-secure Fernet key
suitable for the MASTER_ENCRYPTION_KEY environment variable.

Usage::

    python -m scripts.generate_encryption_key
    python scripts/generate_encryption_key.py
"""

import argparse
import sys

from cryptography.fernet import Fernet


def main() -> None:
    """Generate and print a new Fernet encryption key with setup instructions."""
    parser = argparse.ArgumentParser(
        description='Generate a new Fernet encryption key for ZenGrid.',
        epilog=(
            'The generated key should be stored securely and set as the '
            'MASTER_ENCRYPTION_KEY environment variable before starting '
            'the ZenGrid application.'
        ),
    )
    parser.add_argument(
        '--bare',
        action='store_true',
        help='Print only the raw key (no instructions). Useful for piping.',
    )
    args = parser.parse_args()

    key: str = Fernet.generate_key().decode('utf-8')

    if args.bare:
        print(key)
        return

    print()
    print('=' * 60)
    print('  ZenGrid — New Fernet Encryption Key')
    print('=' * 60)
    print()
    print(f'  {key}')
    print()
    print('-' * 60)
    print('  Setup Instructions')
    print('-' * 60)
    print()
    print('  1. Copy the key above.')
    print()
    print('  2. Set it as an environment variable:')
    print()
    print('     Linux / macOS:')
    print(f'       export MASTER_ENCRYPTION_KEY="{key}"')
    print()
    print('     Windows (PowerShell):')
    print(f'       $env:MASTER_ENCRYPTION_KEY="{key}"')
    print()
    print('     Windows (cmd):')
    print(f'       set MASTER_ENCRYPTION_KEY={key}')
    print()
    print('  3. For production, add it to your .env file or')
    print('     secrets manager (never commit it to source control).')
    print()
    print('  4. Restart ZenGrid for the new key to take effect.')
    print()
    print('=' * 60)
    print()


if __name__ == '__main__':
    main()
