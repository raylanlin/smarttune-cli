# Contributing

Thanks for your interest in contributing to SmartTune CLI!

## Getting Started

```bash
git clone https://github.com/raylanlin/smarttune-cli.git
cd smarttune-cli
pip install -e ".[dev,all]"
```

## Development Workflow

1. Fork the repo and create a feature branch
2. Write your changes
3. Run tests: `pytest tests/ -v`
4. Lint: `ruff check smarttune/ && black --check smarttune/`
5. Submit a PR

## Code Style

- Python 3.9+ with type hints
- Black (line length 100)
- Ruff for linting
- Descriptive docstrings for public APIs

## Adding a New Platform Adapter

See the [README](README.md#extending-with-a-new-platform) for the 3-step guide.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
