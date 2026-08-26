# Known issues

## mypy tests failing locally (2026-08-09)

Five tests that shell out to mypy fail with "DLL load failed while
importing mypy: An Application Control policy has blocked this file."
This is a Windows Application Control policy on the development
machine blocking mypy's compiled extension, not a code defect - the
same tests passed earlier in the session and ruff still runs clean.

The tests are deliberately NOT skipped. They verify that generated
code type-checks, which is this project's central contract; a
skip-on-unavailable would hide exactly the signal they exist to
provide.

Resolution: run them on a machine without the policy, or in CI
(SFM-11). Do not trust the type-checking claim until they pass.

The affected tests:

- `tests/test_fields.py::test_mypy_passes_clean_on_the_module`
- `tests/test_generate.py::test_generated_output_passes_mypy`
- `tests/test_generate.py::test_generated_fields_are_statically_resolvable`
- `tests/test_project.py::test_the_generated_module_type_checks`
- `tests/test_query.py::test_mypy_passes_clean_on_the_module`

`pytest.exe` is blocked by the same policy; run the suite with
`uv run python -m pytest`.
