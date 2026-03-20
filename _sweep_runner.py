"""Sweep runner: loads override env file, then runs backtest."""
import os
import sys

sweep_env = os.environ.get("SWEEP_RUNTIME_ENV_PATH")
if sweep_env:
    import env_paths
    env_paths.PUBLIC_RUNTIME_ENV_PATH = sweep_env

from backtest import main
main()
