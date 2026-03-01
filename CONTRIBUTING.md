# Contributing

Thanks for your interest in contributing to nexus-collection-dl.

## Reporting bugs

Use the [Bug Report](https://github.com/scottmccarrison/nexus-collection-dl/issues/new?template=bug_report.yml) issue template. Include steps to reproduce, your OS, Python version, and any error output. The more detail, the faster we can fix it.

## Suggesting features

Use the [Feature Request](https://github.com/scottmccarrison/nexus-collection-dl/issues/new?template=feature_request.yml) issue template. Describe the feature and why you need it.

## Asking questions

Head to [Discussions](https://github.com/scottmccarrison/nexus-collection-dl/discussions) for questions, troubleshooting, and general conversation. Issues are for bugs and feature requests only.

## Development setup

```bash
git clone https://github.com/scottmccarrison/nexus-collection-dl.git
cd nexus-collection-dl
./setup.sh
source venv/bin/activate
```

You'll need a Nexus Mods API key to test against the API. See the [README](README.md#setup) for details.

## Pull requests

- Create a feature branch off `main`
- Use a descriptive branch name (e.g., `fix-rar-extraction`, `add-dry-run-flag`)
- Keep changes focused. One PR per feature or fix.
- Link the related issue if there is one
- Test your changes locally before submitting

That's it. Nothing complicated.
