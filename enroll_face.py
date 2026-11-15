"""Enroll the owner's face for Face guard. Same as `jarvis enroll-face` (see jarvis/enroll.py)."""
import sys

from jarvis.enroll import main

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
