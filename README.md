# eodhp-ades-stagein

### Repo to containerise the stagein script used in the EODHP Workflow Runner

The code in this repo is based on that included in the EOEPCA project. It handles the harvesting of STAC items in preparation for workflow execution, creating a STAC Catalog that can then be ingested by subsequent workflow steps.

# Development of this component

## Getting started

You will need Python 3.13 and [uv](https://docs.astral.sh/uv/).

## Install via makefile

```commandline
make setup
```

This will sync dependencies with `uv sync` and install `pre-commit` hooks.

It's safe and fast to run `make setup` repeatedly as it will only update these things if
they have changed.

After `make setup` you can run `make pre-commit` to run pre-commit checks on staged changes and
`make pre-commit-all` to run them on all files. This replicates the linter checks that
run from GitHub actions.

## Building and testing

This component uses `pytest` tests and `ruff` for linting and formatting.

A number of `make` targets are defined:
* `make test`: run tests continuously (using pytest-watcher)
* `make testonce`: run tests once
* `make format`: auto-fix lint issues and format code
* `make check`: run all checks (ruff, pyright, validate-pyproject)
* `make dockerbuild`: build a `latest` Docker image (use `make dockerbuild VERSION=1.2.3` for a release image)
* `make dockerpush`: push a `latest` Docker image (again, you can add `VERSION=1.2.3`) - normally this should be done
  only via the build system and its GitHub actions.

## Managing requirements

Dependencies are specified in `pyproject.toml`. After changing them:

* Run `uv sync` to update the lockfile and install dependencies.
* Run `make testonce` to verify everything works.
* Commit `pyproject.toml` and `uv.lock`.

## Releasing

Ensure that `make check` and `make testonce` work correctly and produce no errors before
continuing.

Releases tagged `latest` and targeted at development environments can be created from the `main` branch. Releases for
installation in non-development environments should be created from a Git tag named using semantic versioning. For
example, using

* `git tag v1.2.3`
* `git push --tags`

Docker images will be built automatically by GitHub Actions after pushing to the EO-DataHub repos.
