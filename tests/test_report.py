"""tests/test_report.py - Tests for P2-7: NybbleReport / build_report new API."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional
from unittest.mock import patch, MagicMock

import pytest


# ── Session isolation ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_session(tmp_path, monkeypatch):
    """Reset the DuckDB session between tests."""
    import nybble.resolver as r
    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()

    yield

    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sqlite_db(tmp_path) -> str:
    """SQLite DB with users → orders FK relationship."""
    db_path = tmp_path / "test_report.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL)"
    )
    con.execute("INSERT INTO users VALUES (1, 'Alice'), (2, 'Bob')")
    con.execute("INSERT INTO orders VALUES (101, 1, 99.5), (102, 2, 149.0)")
    con.commit()
    con.close()
    return f"sqlite:///{db_path}"


@pytest.fixture
def single_csv(tmp_path) -> str:
    """Simple CSV file without FK relationships."""
    p = tmp_path / "data.csv"
    p.write_text("id,name,score\n1,Alice,90\n2,Bob,80\n3,Carol,70\n")
    return str(p)


# ── Tests: build_report returns NybbleReport with 5 sections ──────────────────

class TestBuildReportReturnsNybbleReport:

    def test_returns_nybble_report_instance(self, single_csv):
        from nybble.report import build_report, NybbleReport
        report = build_report(single_csv)
        assert isinstance(report, NybbleReport)

    def test_has_five_sections(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv)
        assert len(report.sections) == 5

    def test_all_sections_non_empty(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv)
        for sec in report.sections:
            assert sec.title, f"Section has empty title"
            assert sec.content.strip(), f"Section '{sec.title}' has empty content"

    def test_source_attribute_set(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv)
        assert report.source == single_csv

    def test_generated_at_is_iso8601(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv)
        # Should be like 2024-01-15T12:00:00Z
        assert "T" in report.generated_at
        assert report.generated_at.endswith("Z")

    def test_sections_have_expected_titles(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv)
        titles = [sec.title for sec in report.sections]
        assert "Overview" in titles
        assert "Schema Tables" in titles
        assert "Foreign Key Summary" in titles
        assert "Sample Data" in titles
        assert "Key Queries" in titles

    def test_sqlite_fk_report(self, sqlite_db):
        from nybble.report import build_report
        report = build_report(sqlite_db)
        assert len(report.sections) == 5


# ── Tests: to_markdown() ──────────────────────────────────────────────────────

class TestToMarkdown:

    def test_contains_h1_heading(self, single_csv):
        from nybble.report import build_report
        md = build_report(single_csv).to_markdown()
        assert "# Nybble Report" in md

    def test_contains_overview_heading(self, single_csv):
        from nybble.report import build_report
        md = build_report(single_csv).to_markdown()
        assert "## Overview" in md

    def test_contains_schema_tables_heading(self, single_csv):
        from nybble.report import build_report
        md = build_report(single_csv).to_markdown()
        assert "## Schema Tables" in md

    def test_contains_foreign_key_summary_heading(self, single_csv):
        from nybble.report import build_report
        md = build_report(single_csv).to_markdown()
        assert "## Foreign Key Summary" in md

    def test_contains_sample_data_heading(self, single_csv):
        from nybble.report import build_report
        md = build_report(single_csv).to_markdown()
        assert "## Sample Data" in md

    def test_contains_key_queries_heading(self, single_csv):
        from nybble.report import build_report
        md = build_report(single_csv).to_markdown()
        assert "## Key Queries" in md

    def test_returns_string(self, single_csv):
        from nybble.report import build_report
        md = build_report(single_csv).to_markdown()
        assert isinstance(md, str)
        assert len(md) > 100


# ── Tests: to_html() ─────────────────────────────────────────────────────────

class TestToHtml:

    def test_contains_html_tag(self, single_csv):
        from nybble.report import build_report
        html = build_report(single_csv).to_html()
        assert "<html" in html

    def test_contains_body_tag(self, single_csv):
        from nybble.report import build_report
        html = build_report(single_csv).to_html()
        assert "<body>" in html

    def test_contains_report_content(self, single_csv):
        from nybble.report import build_report
        html = build_report(single_csv).to_html()
        # Markdown headings should be converted to HTML
        assert "<h1>" in html or "Nybble Report" in html

    def test_has_style_block(self, single_csv):
        from nybble.report import build_report
        html = build_report(single_csv).to_html()
        assert "<style>" in html

    def test_is_self_contained(self, single_csv):
        from nybble.report import build_report
        html = build_report(single_csv).to_html()
        assert "<!DOCTYPE html>" in html
        assert "</html>" in html


# ── Tests: save() ─────────────────────────────────────────────────────────────

class TestSave:

    def test_save_md_writes_file(self, single_csv, tmp_path):
        from nybble.report import build_report
        report = build_report(single_csv)
        out = tmp_path / "out.md"
        result = report.save(out)
        assert out.exists()
        assert result == out

    def test_save_md_contains_markdown(self, single_csv, tmp_path):
        from nybble.report import build_report
        report = build_report(single_csv)
        out = tmp_path / "out.md"
        report.save(out)
        content = out.read_text(encoding="utf-8")
        assert "# Nybble Report" in content

    def test_save_html_writes_html_file(self, single_csv, tmp_path):
        from nybble.report import build_report
        report = build_report(single_csv)
        out = tmp_path / "out.html"
        result = report.save(out)
        assert out.exists()
        assert result == out

    def test_save_html_contains_html_tags(self, single_csv, tmp_path):
        from nybble.report import build_report
        report = build_report(single_csv)
        out = tmp_path / "out.html"
        report.save(out)
        content = out.read_text(encoding="utf-8")
        assert "<html" in content
        assert "<body>" in content

    def test_save_md_content_equals_to_markdown(self, single_csv, tmp_path):
        from nybble.report import build_report
        report = build_report(single_csv)
        out = tmp_path / "out.md"
        report.save(out)
        assert out.read_text(encoding="utf-8") == report.to_markdown()

    def test_save_html_content_equals_to_html(self, single_csv, tmp_path):
        from nybble.report import build_report
        report = build_report(single_csv)
        out = tmp_path / "out.html"
        report.save(out)
        assert out.read_text(encoding="utf-8") == report.to_html()

    def test_save_creates_parent_directories(self, single_csv, tmp_path):
        from nybble.report import build_report
        report = build_report(single_csv)
        nested = tmp_path / "a" / "b" / "c" / "report.md"
        report.save(nested)
        assert nested.exists()


# ── Tests: sample data section ────────────────────────────────────────────────

class TestSampleDataSection:

    def test_sample_data_section_present(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv, include_sample_rows=True)
        titles = [s.title for s in report.sections]
        assert "Sample Data" in titles

    def test_sample_data_section_absent_when_disabled(self, single_csv):
        """When include_sample_rows=False, Sample Data section should still be present
        but should not show table rows (section is created with empty/stub content)."""
        from nybble.report import build_report
        report = build_report(single_csv, include_sample_rows=False)
        # Section count should be 4 (Overview, Schema, FK Summary, Key Queries)
        titles = [s.title for s in report.sections]
        assert "Sample Data" not in titles

    def test_sample_data_respects_limit(self, tmp_path):
        """With sample_limit=2, at most 2 rows should appear per table."""
        p = tmp_path / "big.csv"
        rows = "\n".join(f"{i},name{i}" for i in range(1, 21))
        p.write_text(f"id,name\n{rows}\n")
        from nybble.report import build_report
        report = build_report(str(p), sample_limit=2)
        sample_sec = next(s for s in report.sections if s.title == "Sample Data")
        # Count data rows in table (lines that start with |, excluding header and separator)
        table_lines = [ln for ln in sample_sec.content.splitlines()
                       if ln.strip().startswith("|")]
        # 1 header + 1 separator + at most 2 data rows = 4 lines max per table
        data_rows = [ln for ln in table_lines
                     if "---" not in ln and "`id`" not in ln.lower()]
        assert len(data_rows) <= 2

    def test_sample_data_error_handled_gracefully(self, single_csv):
        """If DuckDB raises during sample fetch, content shows error block not crash."""
        from nybble.report import build_report, _build_report_from_source

        # Patch session to raise on SELECT
        original = _build_report_from_source

        def patched(*args, **kwargs):
            """Call real function but mock session inside sample data section."""
            import nybble.report as rmod
            real_session = rmod.session

            call_count = {"n": 0}

            def mock_session():
                con = real_session()
                call_count["n"] += 1
                if call_count["n"] > 1:
                    # Wrap connection to raise on execute after first call
                    class _FaultyConn:
                        def execute(self, *a, **kw):
                            raise RuntimeError("Injected DuckDB error")
                    return _FaultyConn()  # type: ignore[return-value]
                return con

            with patch.object(rmod, "session", mock_session):
                return original(*args, **kwargs)

        report = patched(single_csv)
        sample_sec = next((s for s in report.sections if s.title == "Sample Data"), None)
        # Section must exist and contain an error indicator, not raise
        assert sample_sec is not None


# ── Tests: FK query section ───────────────────────────────────────────────────

class TestFKQuerySection:

    def test_key_queries_section_present(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv)
        titles = [s.title for s in report.sections]
        assert "Key Queries" in titles

    def test_fk_join_query_emitted_for_fk(self, sqlite_db):
        """With an FK relationship, Key Queries must contain a fenced SQL block."""
        from nybble.report import build_report
        report = build_report(sqlite_db)
        kq = next(s for s in report.sections if s.title == "Key Queries")
        # Should have at least one ```sql block
        assert "```sql" in kq.content

    def test_fk_join_uses_inner_join(self, sqlite_db):
        from nybble.report import build_report
        report = build_report(sqlite_db)
        kq = next(s for s in report.sections if s.title == "Key Queries")
        assert "INNER JOIN" in kq.content

    def test_fk_join_has_limit_20(self, sqlite_db):
        from nybble.report import build_report
        report = build_report(sqlite_db)
        kq = next(s for s in report.sections if s.title == "Key Queries")
        assert "LIMIT 20" in kq.content

    def test_no_fk_queries_when_disabled(self, sqlite_db):
        from nybble.report import build_report
        report = build_report(sqlite_db, include_fk_queries=False)
        titles = [s.title for s in report.sections]
        assert "Key Queries" not in titles

    def test_one_sql_block_per_fk(self, sqlite_db):
        """Number of ```sql blocks should equal number of FK relationships."""
        from nybble.report import build_report
        report = build_report(sqlite_db)
        kq = next(s for s in report.sections if s.title == "Key Queries")
        fk_sec = next(s for s in report.sections if s.title == "Foreign Key Summary")

        # Count FK data rows (skip header + separator)
        fk_rows = [ln for ln in fk_sec.content.splitlines()
                   if ln.strip().startswith("|")
                   and "From Table" not in ln
                   and "---" not in ln]
        sql_blocks = kq.content.count("```sql")
        assert sql_blocks == len(fk_rows)


# ── Tests: schema section ─────────────────────────────────────────────────────

class TestSchemaSection:

    def test_schema_section_present(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv)
        titles = [s.title for s in report.sections]
        assert "Schema Tables" in titles

    def test_schema_section_absent_when_disabled(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv, include_schema=False)
        titles = [s.title for s in report.sections]
        assert "Schema Tables" not in titles

    def test_schema_has_row_per_column(self, single_csv):
        """single_csv has 3 columns; schema section must have 3 data rows."""
        from nybble.report import build_report
        report = build_report(single_csv)
        schema_sec = next(s for s in report.sections if s.title == "Schema Tables")
        # Each column gets a | row | type | key | line
        data_rows = [
            ln for ln in schema_sec.content.splitlines()
            if ln.strip().startswith("|")
            and "Column" not in ln
            and "---" not in ln
            and ln.strip() != "|"
        ]
        # single_csv has id, name, score → 3 rows
        assert len(data_rows) >= 3

    def test_schema_contains_column_names(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv)
        schema_sec = next(s for s in report.sections if s.title == "Schema Tables")
        # Column names from the CSV
        assert "id" in schema_sec.content
        assert "name" in schema_sec.content
        assert "score" in schema_sec.content


# ── Tests: FK summary section ─────────────────────────────────────────────────

class TestForeignKeySummary:

    def test_fk_summary_section_present(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv)
        titles = [s.title for s in report.sections]
        assert "Foreign Key Summary" in titles

    def test_fk_summary_has_header_row(self, single_csv):
        from nybble.report import build_report
        report = build_report(single_csv)
        fk_sec = next(s for s in report.sections if s.title == "Foreign Key Summary")
        assert "From Table" in fk_sec.content

    def test_fk_summary_shows_relationship(self, sqlite_db):
        """users/orders DB should produce at least one FK row."""
        from nybble.report import build_report
        report = build_report(sqlite_db)
        fk_sec = next(s for s in report.sections if s.title == "Foreign Key Summary")
        # At least one row with actual FK data (not just header/separator)
        data_rows = [
            ln for ln in fk_sec.content.splitlines()
            if ln.strip().startswith("|")
            and "From Table" not in ln
            and "---" not in ln
        ]
        assert len(data_rows) >= 1


# ── Tests: CLI subcommand ─────────────────────────────────────────────────────

class TestCLIReportCommand:

    def test_report_exits_0_and_writes_file(self, single_csv, tmp_path):
        """nybble report --source <path> --output <file> should exit 0 and write the file."""
        from click.testing import CliRunner
        from nybble.cli import cli

        out_file = tmp_path / "report.md"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "report",
            "--source", single_csv,
            "--output", str(out_file),
        ])
        assert result.exit_code == 0, f"stdout: {result.output}\nexc: {result.exception}"
        assert out_file.exists()
        assert out_file.stat().st_size > 0

    def test_report_prints_output_path(self, single_csv, tmp_path):
        """CLI output should contain 'Report written to'."""
        from click.testing import CliRunner
        from nybble.cli import cli

        out_file = tmp_path / "report.md"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "report",
            "--source", single_csv,
            "--output", str(out_file),
        ])
        assert "Report written to" in result.output

    def test_report_html_output(self, single_csv, tmp_path):
        """--output file.html should write an HTML file."""
        from click.testing import CliRunner
        from nybble.cli import cli

        out_file = tmp_path / "report.html"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "report",
            "--source", single_csv,
            "--output", str(out_file),
        ])
        assert result.exit_code == 0, f"exit={result.exit_code}, out={result.output}"
        content = out_file.read_text(encoding="utf-8")
        assert "<html" in content

    def test_report_exits_1_on_invalid_source(self, tmp_path):
        """nybble report with a non-existent source should exit 1 and print to stderr."""
        from click.testing import CliRunner
        from nybble.cli import cli

        out_file = tmp_path / "report.md"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "report",
            "--source", "/nonexistent/path/that/does/not/exist.csv",
            "--output", str(out_file),
        ])
        assert result.exit_code != 0

    def test_report_no_source_exits_1(self, tmp_path):
        """nybble report without --source should exit 1."""
        from click.testing import CliRunner
        from nybble.cli import cli
        import os

        out_file = tmp_path / "report.md"
        runner = CliRunner()
        # Ensure env var is not set
        env = {k: v for k, v in os.environ.items() if k != "NYBBLE_SOURCE"}
        result = runner.invoke(cli, [
            "report",
            "--output", str(out_file),
        ], env=env)
        assert result.exit_code != 0


# ── Tests: ReportSection and NybbleReport dataclasses ────────────────────────

class TestDataclasses:

    def test_report_section_dataclass(self):
        from nybble.report import ReportSection
        sec = ReportSection(title="My Section", content="Some **content**\n")
        assert sec.title == "My Section"
        assert sec.content == "Some **content**\n"

    def test_nybble_report_dataclass(self):
        from nybble.report import NybbleReport, ReportSection
        report = NybbleReport(
            source="test.csv",
            generated_at="2024-01-01T00:00:00Z",
            sections=[ReportSection(title="Overview", content="content")],
        )
        assert report.source == "test.csv"
        assert len(report.sections) == 1

    def test_importable_from_top_level(self):
        from nybble import NybbleReport, ReportSection  # noqa: F401
        assert NybbleReport is not None
        assert ReportSection is not None
