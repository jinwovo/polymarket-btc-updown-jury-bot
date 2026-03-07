# Contributing

Thanks for contributing.

## Development setup

1. Clone and install dependencies.

```bash
pip install -r requirements.txt
npm install
```

2. Run the app stack.

```bash
npm run start
```

3. Open `http://127.0.0.1:3100`.

## Branch and commit rules

- Create a feature branch from `main`.
- Use small, focused commits.
- Write clear commit messages (what changed and why).

Suggested format:

`type(scope): summary`

Examples:
- `feat(dashboard): add paper trade history modal`
- `fix(db): handle sqlite migration ordering`

## Pull request checklist

- [ ] Change is scoped and explained.
- [ ] No unrelated files included.
- [ ] `npm run build` passes.
- [ ] Python syntax checks pass:
  - `python -m py_compile dashboard_server.py`
  - (plus changed Python files)
- [ ] README/docs updated if behavior changed.

## Code guidelines

- Keep logic deterministic where possible.
- Avoid hidden global side effects.
- Prefer defensive checks for missing/partial market data.
- Keep UI polling efficient (avoid request storms).

## Reporting bugs

Please use the Bug Report issue template and include:

- exact command used,
- environment (`python --version`, `node --version`),
- relevant logs,
- reproducible steps.
