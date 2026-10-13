"""Provision a support role for an existing login identity; no password output."""
import argparse
from app.support.runtime import get_store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id")
    parser.add_argument("role", choices=["agent", "admin"])
    args = parser.parse_args()
    get_store().seed_staff(args.user_id, args.role)
    print(f"Support role configured: {args.user_id} / {args.role}")


if __name__ == "__main__":
    main()
