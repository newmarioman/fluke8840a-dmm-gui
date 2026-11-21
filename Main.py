#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FLUKE 8840A Digital Multimeter (DMM) GUI Application

This is the main entry point for the modular FLUKE 8840A DMM graphical user interface.
It handles environment validation, command-line argument parsing, and initializes
the main Tkinter application loop.

Metadata
--------
Author:    Lorenzo Mariani
Email:     newmarioman@hotmail.it
GitHub:    https://github.com/newmarioman
LinkedIn:  https://www.linkedin.com/in/newmarioman
Date:      October 2026
Version:   1.0.0-dev
License:   MIT
"""

import sys
# Early Python version check to prevent syntax/import issues on older runtimes
if sys.version_info < (3, 10):  # noqa: UP036
    sys.stderr.write("Error: Python 3.10 or later is required.\n")
    sys.stderr.write(f"Current version: {sys.version}\n")
    sys.exit(1)

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser( description="Fluke 8840A DMM GUI")
    parser.add_argument("--demo", action="store_true", 
                        help="Use a simulated DMM without hardware")
    parser.add_argument("--no-sound", action="store_true", 
                        help="Disable notification sounds")
    parser.add_argument("--windowed", action="store_true", 
                        help="Start in a resizable window")
    args = parser.parse_args()

    try:
        import tkinter as tk

        from fluke8840a.app import DmmApplication
        from fluke8840a.config import AppConfig

        root = tk.Tk()
        root.withdraw()
        # Initialize and launch the main application
        application = DmmApplication( root, AppConfig(), demo=args.demo, sound=not args.no_sound, maximize=not args.windowed)
        application.run()
    except ImportError as exc:
        print(f"Missing dependency: {exc}. Run: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    except tk.TclError as exc:
        print(f"Could not start the GUI: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
