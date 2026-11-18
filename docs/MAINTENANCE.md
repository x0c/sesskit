# Maintenance (SessKit maintainers)

## Release verification

Install users can stop at [Install](../README.md#install): SessKit is not on PyPI yet.

Maintainers verify an existing release without trusting the working tree:

```bash
python scripts/publish.py verify --version 0.2.23 --check-only
```

Expected `v0.2.23` asset hashes (also recorded by the GitHub release API):

- wheel `sesskit-0.2.23-py3-none-any.whl`: `sha256:30fcf7e063c29d594262a8223b368af2d0e6bd8958e3b65792ada2d9a8964d81`
- sdist `sesskit-0.2.23.tar.gz`: `sha256:acc70a9033f0f9c945ad4f0df53600f61ff5ea5417580df5c930410c2d76f084`

Local publish is not CI-only. Validate first, then upload with credentials
from the environment only — the helper has no password/token options, so
secrets never appear on argv or in shell history. Store the PyPI token once
in your system secret store (prompts for the value; nothing secret is typed
into the command line), then reference it at upload time:

```bash
python scripts/publish.py check-dist --version 0.2.23 --dist dist
security add-generic-password -s pypi-sesskit -a __token__ -w
TWINE_USERNAME=__token__ TWINE_PASSWORD="$(security find-generic-password -s pypi-sesskit -w)" \
  python scripts/publish.py upload --version 0.2.23 --dist dist --confirm-upload
```

(On Linux use your secret store instead, e.g. `secret-tool lookup server
pypi.org`, keeping the same pattern: no literal secret in the command.)

CI publish runs [`.github/workflows/publish-pypi.yml`](../.github/workflows/publish-pypi.yml)
on a `v*` tag push (or manual dispatch of an existing tag) via PyPI Trusted
Publishing — no stored API token (GitHub mints a short-lived OIDC credential
per run).

## Version single-source

`pyproject.toml` `version` is the source. Every release must sync it together with
`src/sesskit/__init__.py` `__version__`, the pinned wheel URLs in `README.md` /
`README.zh-CN.md` / `docs/INTEGRATION_GUIDE.md`, and the assertion in
`tests/test_examples.py::test_readmes_use_same_published_wheel_and_route_to_examples`.
A drift here (e.g. README still pinned `0.2.21` while `pyproject.toml` was `0.2.23`,
fixed 2026-11-02) breaks the install snippet and the test at once.
