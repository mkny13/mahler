# Mahler SDK runner

This standalone package supplies `mahler-sdk-run`, the Claude Agent SDK subprocess
used by Mahler's `agent-sdk` platform (DESIGN D41). It requires Python 3.14 or newer
and is installed separately from Mahler's standard-library-only conductor.

From the repository root, install with:

```sh
uv tool install ./sdk_runner
mahler-sdk-run --help
```

For an authorized inference run, the command accepts:

```sh
mahler-sdk-run --prompt-file prompt.txt --cwd /path/to/project --model MODEL
```

The runtime reads `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` from its environment.
Mahler supplies these through its credits gateway. The runner emits JSON messages
and a final result on stdout, exiting nonzero on failure. See `--help` for optional
budget, turn and tool controls. Actual queries can consume API credits.

Run `/bin/sh scripts/verify_sdk_runner.sh` from the repository to verify packaging.
It builds a wheel in temporary storage, installs it with `--no-deps` in a fresh
virtual environment, validates installed metadata and the entry point, and runs
`--help` from an unrelated directory with a clean environment and temporary HOME.
It cleans up on success or failure and does not query an API or require credentials.
The build may download the declared setuptools build requirements; the gate does
not install or test the Claude Agent SDK dependency or inference behavior.
