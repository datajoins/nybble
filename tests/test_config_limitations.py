"""tests/test_config_limitations.py - Tests for the four source-level limitations.

Covers:
  Fix 2 - tables: key in databases entry raises NotImplementedError
  Fix 3 - semantic_layer.enabled: true raises UserWarning
  Fix 4 - history.max_lines is configurable and validated
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest


# ── Shared session/history fixture ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_session(tmp_path, monkeypatch):
    """Reset the DuckDB session and redirect .nybble/ to a temp dir."""
    import nybble.resolver as r
    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()

    import nybble.arena as arena
    monkeypatch.setattr(arena, "_HISTORY_FILE", tmp_path / ".nybble" / "history.jsonl")
    monkeypatch.setattr(arena, "_QUERIES_FILE", tmp_path / ".nybble" / "queries.json")

    yield

    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()


# ── Fix 2: tables: key raises NotImplementedError ─────────────────────────────

class TestTablesKeyRaisesNotImplementedError:

    def _write_config(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "nybble.yaml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_tables_key_raises_not_implemented(self, tmp_path):
        """A databases entry with a tables: list must raise NotImplementedError."""
        cfg_file = self._write_config(tmp_path, """
version: "1.0"
databases:
  - id: mydb
    type: postgresql
    connection_string: "postgresql://localhost/test"
    tables:
      - users
      - orders
""")
        from nybble.config import load_config
        with pytest.raises(NotImplementedError) as exc_info:
            load_config(cfg_file)
        assert "tables:" in str(exc_info.value)
        assert "not yet implemented" in str(exc_info.value)
        assert "roadmap P2" in str(exc_info.value)
        assert "Remove the 'tables:' key" in str(exc_info.value)

    def test_tables_key_error_message_includes_db_id(self, tmp_path):
        """The error message must include the database id for easy diagnosis."""
        cfg_file = self._write_config(tmp_path, """
version: "1.0"
databases:
  - id: analytics_db
    type: postgresql
    connection_string: "postgresql://localhost/analytics"
    tables:
      - events
""")
        from nybble.config import load_config
        with pytest.raises(NotImplementedError) as exc_info:
            load_config(cfg_file)
        assert "analytics_db" in str(exc_info.value)

    def test_no_tables_key_does_not_raise(self, tmp_path):
        """A databases entry without tables: must not raise."""
        cfg_file = self._write_config(tmp_path, """
version: "1.0"
databases:
  - id: mydb
    type: postgresql
    connection_string: "postgresql://localhost/test"
""")
        from nybble.config import load_config
        # Should not raise - connection will fail but that happens at apply_config time
        cfg = load_config(cfg_file)
        assert cfg.databases[0].id == "mydb"

    def test_empty_tables_list_does_not_raise(self, tmp_path):
        """An empty tables: [] entry must not raise (falsy check)."""
        cfg_file = self._write_config(tmp_path, """
version: "1.0"
databases:
  - id: mydb
    type: postgresql
    connection_string: "postgresql://localhost/test"
    tables: []
""")
        from nybble.config import load_config
        cfg = load_config(cfg_file)
        assert cfg.databases[0].id == "mydb"


# ── Fix 3: semantic_layer.enabled: true warns with UserWarning ─────────────────

class TestSemanticLayerEnabledWarns:

    def _write_config(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "nybble.yaml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_semantic_layer_enabled_raises_user_warning(self, tmp_path):
        """semantic_layer.enabled: true must emit a UserWarning."""
        cfg_file = self._write_config(tmp_path, """
version: "1.0"
graph_engine:
  semantic_layer:
    enabled: true
""")
        from nybble.config import load_config
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_config(cfg_file)
        user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
        assert len(user_warnings) >= 1, "Expected at least one UserWarning"
        messages = [str(w.message) for w in user_warnings]
        assert any("semantic_layer.enabled" in m for m in messages), (
            f"Expected 'semantic_layer.enabled' in warning messages, got: {messages}"
        )

    def test_semantic_layer_enabled_message_mentions_ignored(self, tmp_path):
        """The warning message must mention the flag will be ignored."""
        cfg_file = self._write_config(tmp_path, """
version: "1.0"
graph_engine:
  semantic_layer:
    enabled: true
""")
        from nybble.config import load_config
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_config(cfg_file)
        messages = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
        assert any("ignored" in m.lower() for m in messages)

    def test_semantic_layer_disabled_no_warning(self, tmp_path):
        """semantic_layer.enabled: false must not emit a UserWarning."""
        cfg_file = self._write_config(tmp_path, """
version: "1.0"
graph_engine:
  semantic_layer:
    enabled: false
""")
        from nybble.config import load_config
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_config(cfg_file)
        user_warnings = [
            w for w in caught
            if issubclass(w.category, UserWarning)
            and "semantic_layer" in str(w.message)
        ]
        assert len(user_warnings) == 0

    def test_semantic_layer_absent_no_warning(self, tmp_path):
        """When semantic_layer key is absent, no UserWarning must be emitted."""
        cfg_file = self._write_config(tmp_path, """
version: "1.0"
""")
        from nybble.config import load_config
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_config(cfg_file)
        user_warnings = [
            w for w in caught
            if issubclass(w.category, UserWarning)
            and "semantic_layer" in str(w.message)
        ]
        assert len(user_warnings) == 0


# ── Fix 4: history.max_lines configurable ─────────────────────────────────────

class TestHistoryMaxLinesConfigurable:

    def test_history_rotates_at_custom_cap(self, tmp_path, monkeypatch):
        """Setting _HISTORY_MAX_LINES to 5 must cause rotation after 5 entries."""
        import nybble.arena as arena
        monkeypatch.setattr(arena, "_HISTORY_MAX_LINES", 5)

        from nybble import load, query
        csv = tmp_path / "t.csv"
        csv.write_text("id,val\n1,a\n2,b\n")
        load(str(csv), name="t")

        for i in range(8):
            query(f"SELECT {i}")

        lines = (tmp_path / ".nybble" / "history.jsonl").read_text().splitlines()
        assert len(lines) <= 5, f"Expected ≤5 history lines after rotation, got {len(lines)}"

    def test_reload_history_cap_reads_yaml(self, tmp_path, monkeypatch):
        """_reload_history_cap() reads history.max_lines from nybble.yaml."""
        import nybble.arena as arena
        # Reset to default first
        monkeypatch.setattr(arena, "_HISTORY_MAX_LINES", 10_000)

        cfg = tmp_path / "nybble.yaml"
        cfg.write_text("history:\n  max_lines: 42\n", encoding="utf-8")

        # Point find_config to our tmp dir
        from nybble.config import find_config as _orig_find
        monkeypatch.setattr(
            "nybble.config.find_config",
            lambda start_dir=None: cfg,
        )
        import nybble.config as config_mod
        monkeypatch.setattr(config_mod, "find_config", lambda start_dir=None: cfg)

        # Also patch arena's import of find_config at call time
        import nybble.arena as arena_mod
        original_reload = arena_mod._reload_history_cap

        # Call through the actual implementation but with patched find_config
        def patched_find_config(start_dir=None):
            return cfg

        import nybble.config as _cfg_mod
        old_find = _cfg_mod.find_config
        _cfg_mod.find_config = patched_find_config
        try:
            result = arena_mod._reload_history_cap()
        finally:
            _cfg_mod.find_config = old_find
        assert result == 42
        assert arena_mod._HISTORY_MAX_LINES == 42

    def test_reload_history_cap_invalid_raises_value_error(self, tmp_path):
        """_reload_history_cap() raises ValueError for non-positive values."""
        import nybble.arena as arena_mod
        import nybble.config as _cfg_mod

        cfg = tmp_path / "nybble.yaml"
        cfg.write_text("history:\n  max_lines: -1\n", encoding="utf-8")

        old_find = _cfg_mod.find_config
        _cfg_mod.find_config = lambda start_dir=None: cfg
        try:
            with pytest.raises(ValueError, match="positive integer"):
                arena_mod._reload_history_cap()
        finally:
            _cfg_mod.find_config = old_find

    def test_reload_history_cap_zero_raises_value_error(self, tmp_path):
        """_reload_history_cap() raises ValueError for zero."""
        import nybble.arena as arena_mod
        import nybble.config as _cfg_mod

        cfg = tmp_path / "nybble.yaml"
        cfg.write_text("history:\n  max_lines: 0\n", encoding="utf-8")

        old_find = _cfg_mod.find_config
        _cfg_mod.find_config = lambda start_dir=None: cfg
        try:
            with pytest.raises(ValueError, match="positive integer"):
                arena_mod._reload_history_cap()
        finally:
            _cfg_mod.find_config = old_find

    def test_reload_history_cap_string_value_raises_value_error(self, tmp_path):
        """_reload_history_cap() raises ValueError for non-integer strings."""
        import nybble.arena as arena_mod
        import nybble.config as _cfg_mod

        cfg = tmp_path / "nybble.yaml"
        cfg.write_text("history:\n  max_lines: 'unlimited'\n", encoding="utf-8")

        old_find = _cfg_mod.find_config
        _cfg_mod.find_config = lambda start_dir=None: cfg
        try:
            with pytest.raises(ValueError):
                arena_mod._reload_history_cap()
        finally:
            _cfg_mod.find_config = old_find

    def test_reload_history_cap_absent_key_keeps_default(self, tmp_path):
        """_reload_history_cap() keeps existing value when key is absent."""
        import nybble.arena as arena_mod
        import nybble.config as _cfg_mod

        cfg = tmp_path / "nybble.yaml"
        cfg.write_text("version: '1.0'\n", encoding="utf-8")

        arena_mod._HISTORY_MAX_LINES = 10_000
        old_find = _cfg_mod.find_config
        _cfg_mod.find_config = lambda start_dir=None: cfg
        try:
            result = arena_mod._reload_history_cap()
        finally:
            _cfg_mod.find_config = old_find
        assert result == 10_000


# ── Fix: hyphenated config id → sanitised DuckDB name ────────────────────────

class TestConfigIdSanitisation:
    """apply_config() must sanitise hyphenated (and special-char) id values to
    valid DuckDB identifiers before registering them in the shared session."""

    def _write_config(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "nybble.yaml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_hyphenated_file_id_registered_as_underscore(self, tmp_path):
        """A file id 'medicine-data' must be registered as 'medicine_data'."""
        csv = tmp_path / "data.csv"
        csv.write_text("id,val\n1,a\n2,b\n")

        cfg = self._write_config(tmp_path, f"""\
version: "1.0"
files:
  - id: medicine-data
    type: csv
    path: {csv}
""")
        from nybble.config import load_config, apply_config
        import nybble.resolver as r

        config = load_config(cfg)
        rels = apply_config(config)

        assert len(rels) == 1
        assert rels[0].name == "medicine_data", (
            f"Expected 'medicine_data', got '{rels[0].name}'"
        )
        assert "medicine_data" in r._REGISTERED
        assert "medicine-data" not in r._REGISTERED

    def test_hyphenated_stream_id_registered_as_underscore(self, tmp_path, monkeypatch):
        """A stream id 'my-api' must be registered as 'my_api'."""
        import json
        from nybble import resolver as r

        # Patch _load_url to avoid a real HTTP call
        dummy_data = [{"id": 1, "name": "test"}]

        def _fake_load_url(source, name, headers=None):
            conn = r.session()
            conn.execute(
                f"CREATE OR REPLACE VIEW \"{name}\" AS "
                f"SELECT * FROM (VALUES (1, 'test')) t(id, name)"
            )
            return r.Relation(name=name, source=source, source_type="url")

        monkeypatch.setattr(r, "_load_url", _fake_load_url)

        cfg = self._write_config(tmp_path, """\
version: "1.0"
streams:
  - id: my-api
    type: api
    url: https://example.com/data
    format: json
""")
        from nybble.config import load_config, apply_config

        config = load_config(cfg)
        rels = apply_config(config)

        assert len(rels) == 1
        assert rels[0].name == "my_api", (
            f"Expected 'my_api', got '{rels[0].name}'"
        )
        assert "my_api" in r._REGISTERED
        assert "my-api" not in r._REGISTERED

    def test_plain_id_unchanged(self, tmp_path):
        """An id with no special characters must be registered unchanged."""
        csv = tmp_path / "plain.csv"
        csv.write_text("x,y\n1,2\n")

        cfg = self._write_config(tmp_path, f"""\
version: "1.0"
files:
  - id: plainname
    type: csv
    path: {csv}
""")
        from nybble.config import load_config, apply_config
        import nybble.resolver as r

        config = load_config(cfg)
        rels = apply_config(config)

        assert rels[0].name == "plainname"
        assert "plainname" in r._REGISTERED

    def test_multiple_hyphenated_ids_all_sanitised(self, tmp_path):
        """Multiple files with hyphenated ids must all get underscore names."""
        for fname in ("a-one.csv", "b-two.csv"):
            (tmp_path / fname).write_text("n\n1\n")

        cfg = self._write_config(tmp_path, f"""\
version: "1.0"
files:
  - id: a-one
    type: csv
    path: {tmp_path / "a-one.csv"}
  - id: b-two
    type: csv
    path: {tmp_path / "b-two.csv"}
""")
        from nybble.config import load_config, apply_config
        import nybble.resolver as r

        config = load_config(cfg)
        rels = apply_config(config)

        names = {rel.name for rel in rels}
        assert "a_one" in names
        assert "b_two" in names
        assert "a-one" not in r._REGISTERED
        assert "b-two" not in r._REGISTERED
