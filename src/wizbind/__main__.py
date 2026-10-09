"""One entry point for the supported AP, pairing and control CLI."""

import signal
import sys


def main():
    from .app import main as app_main

    def interrupt(signum, frame):
        # Ignore repeated Ctrl+C while cleanup finishes.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        print("\nCtrl+C detected.", flush=True)
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        return app_main()
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, ValueError, OSError, TypeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
