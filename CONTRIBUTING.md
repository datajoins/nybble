# Contributing to Nybble

Thank you for your interest in contributing to Nybble. This document explains how to
report issues, propose changes, submit pull requests, and the legal terms that govern
contributions to this project.

---

## Table of Contents

1. [Contributor License Agreement](#1-contributor-license-agreement)
2. [Code of Conduct](#2-code-of-conduct)
3. [How to Report a Bug](#3-how-to-report-a-bug)
4. [How to Request a Feature](#4-how-to-request-a-feature)
5. [How to Submit a Pull Request](#5-how-to-submit-a-pull-request)
6. [Development Setup](#6-development-setup)
7. [Coding Standards](#7-coding-standards)
8. [Commit Message Guidelines](#8-commit-message-guidelines)
9. [Tests](#9-tests)

---

## 1. Contributor License Agreement

**By submitting a contribution to this project, you agree to assign copyright of
your contribution to DataJoins.**

This means that while you are credited as the author of your contribution, DataJoins
becomes the copyright owner of the contributed code. You retain the right to use your
own contribution in other projects.

### Why this CLA exists

Nybble is currently licensed under the Apache License 2.0. The CLA preserves the
project maintainer's right to change the license in the future - for example, to apply
a stronger copyleft license (such as AGPL-3.0) to the `server/` and `deploy/`
components if the project reaches a scale where commercial protection is warranted,
while keeping the `nybble/` pip library under a permissive license for maximum
developer adoption.

Without copyright assignment, any future relicensing would require tracking down every
contributor to obtain individual approval - which is impractical. The CLA prevents
that bottleneck.

### What the CLA does NOT do

- It does not prevent you from using your own contribution in other projects.
- It does not claim ownership of code you wrote that you did not contribute to this
  repo.
- It does not restrict your right to fork this project under the terms of the
  Apache 2.0 license.

### How to signal agreement

There is no form to sign. Submitting a pull request constitutes your agreement to these
terms. If you do not agree, please do not submit a pull request.

If you are contributing on behalf of your employer, you confirm that your employer has
authorised the contribution and the copyright assignment.

---

## 2. Code of Conduct

Be direct, respectful, and constructive. Criticism of code and ideas is welcome;
personal attacks are not. Issues and pull requests that contain abusive, harassing, or
deliberately inflammatory content will be closed without response.

---

## 3. How to Report a Bug

1. **Search first.** Check [existing issues](https://github.com/datajoins/nybble/issues)
   to see if the bug has already been reported.
2. **Open a new issue** using the Bug Report template (if available) or with the
   following information:
   - Nybble version (`pip show datajoins-nybble`)
   - Python version and operating system
   - The exact command or code that triggered the bug
   - The full error message or unexpected output
   - What you expected to happen instead
3. **Minimal reproduction.** A small, self-contained example that demonstrates the
   problem makes it much easier to fix. Attach a sample data file if the bug is
   data-specific.

---

## 4. How to Request a Feature

1. Open a [GitHub Discussion](https://github.com/datajoins/nybble/discussions) or
   issue tagged `enhancement`.
2. Describe the use case - what problem does the feature solve? What does the current
   behaviour force you to do instead?
3. If you have a specific API or CLI design in mind, include a short example of how
   you would want to invoke it.

Feature requests are prioritised by how broadly useful they are relative to their
implementation complexity. Nybble deliberately stays small; features that require heavy
new dependencies are unlikely to be merged into the core package.

---

## 5. How to Submit a Pull Request

1. **Fork** the repository and create a branch from `main`.
2. **Name your branch** descriptively: `fix/erd-fk-inference-nulls`,
   `feat/add-mongodb-schema-scan`, `docs/update-readme-quickstart`.
3. **Keep pull requests focused.** One logical change per PR. Split unrelated fixes
   into separate PRs.
4. **Update tests.** If your change affects behaviour, add or update tests in `tests/`.
5. **Run the test suite** before pushing (see [Tests](#9-tests)).
6. **Update documentation** if you changed a CLI flag, public API, or user-visible
   behaviour.
7. **Open the PR** against the `main` branch with a clear title and description
   explaining *what* changed and *why*.
8. Respond to review comments promptly. PRs with no activity for 30 days may be closed.

---

## 6. Development Setup

```bash
# Clone your fork
git clone https://github.com/<your-username>/nybble.git
cd nybble

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"
```

---

## 7. Coding Standards

- **Formatter:** The project uses `ruff` for linting. Run `ruff check .` before
  committing. The configuration is in `pyproject.toml`.
- **Line length:** 120 characters (configured in `pyproject.toml`).
- **Type hints:** Preferred on all new public functions; not required on internal
  helpers.
- **Imports:** Standard library first, third-party second, local imports last.
  Separate each group with a blank line.
- **No new heavy dependencies** in the core `nybble/` package without prior discussion.
  Optional features should use `[project.optional-dependencies]` extras in
  `pyproject.toml`.

---

## 8. Commit Message Guidelines

Use the [Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(<scope>): <short description>

[optional body]
```

Common types: `fix`, `feat`, `docs`, `test`, `refactor`, `chore`.

Examples:
```
fix(erd): handle tables with no primary key during FK inference
feat(cli): add --output-format flag to nybble erd command
docs(readme): add S3 URL example to quickstart section
```

---

## 9. Tests

```bash
# Run the full test suite
pytest

# Run with coverage
pytest --cov=nybble --cov-report=term-missing

# Run a specific test file
pytest tests/test_erd.py -v
```

All pull requests must pass the existing test suite. New behaviour should be covered by
new tests. Tests live in `tests/` and follow the `test_<module>.py` naming convention.

---

*Questions?* Open a [GitHub Discussion](https://github.com/datajoins/nybble/discussions)
or email [hello@datajoins.com](mailto:hello@datajoins.com).
