from __future__ import annotations

from gws_tui.app import Workspace
from gws_tui.env import load_env_file


def main() -> None:
    load_env_file()
    Workspace().run()


if __name__ == "__main__":
    main()
