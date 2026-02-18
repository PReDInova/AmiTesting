"""Tests for scripts.indicator_library -- AFL indicator file management and parsing."""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.indicator_library import (
    parse_indicator_metadata,
    generate_include_block,
    save_indicator,
    read_indicator,
    delete_indicator,
    list_indicators,
    IndicatorMeta,
    IndicatorParam,
    IndicatorInput,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ind_dir(tmp_path):
    """Create a temporary indicators directory."""
    d = tmp_path / "indicators"
    d.mkdir()
    return d


@pytest.fixture
def sample_include_afl():
    """AFL content for an include-style indicator with typeof guards."""
    return '''\
// ===========================================
// Test TEMA Indicator
// ===========================================
//
// This indicator calculates TEMA for smoothing.
//
// INPUTS (must be defined before including this file):
//   - smoothingLength: Period for EMA calculations
//   - sourcePrice: Price array to smooth
//   - sessionResetTime: (OPTIONAL) Time in HHMMSS format
//
// OUTPUTS:
//   - temas: The calculated TEMA array

if( typeof( sessionResetTime ) == "undefined" )
    sessionResetTime = Param( "Session Reset Time", 0, 0, 235959, 1 );

alpha = 2.0 / (smoothingLength + 1);
temas = 3 * ema1 - 3 * ema2 + ema3;
'''


@pytest.fixture
def sample_isempty_afl():
    """AFL content for an indicator using IsEmpty guards."""
    return '''\
// ===========================================
// Range Bound Detection
// ===========================================
//
// Detects range-bound conditions.
//
// INPUTS (can be defined before including this file):
//   - rangePeriod: Number of bars
//
// OUTPUTS:
//   - isRangeBound: Boolean flag
//   - rangeStrength: 0-100 score

if( IsEmpty( rangePeriod ) )
{
    rangePeriod = Param( "Range Period (bars)", 20, 5, 100, 1 );
}

if( IsEmpty( rangeThreshold ) )
{
    rangeThreshold = Param( "Range Threshold", 1.5, 0.5, 5.0, 0.1 );
}

isRangeBound = rangeBoundBars >= minRangeBars;
'''


@pytest.fixture
def sample_standalone_afl():
    """AFL content for a standalone indicator with bare Params and Plot."""
    return '''\
// ===========================================
// ADX Indicator
// ===========================================
//
// Average Directional Index for trend strength.

period = Param("ADX Period", 14, 5, 50, 1);
showDI = ParamToggle("Show +DI/-DI", "No|Yes", 1);
ADXvalue = Wilders(DX, period);
Plot(ADXvalue, "ADX", colorGold, styleLine);
Title = "ADX";
'''


@pytest.fixture
def sample_typeof_toggle_afl():
    """AFL content with typeof-guarded ParamToggle."""
    return '''\
// ===========================================
// Session Filter
// ===========================================

if( typeof( avoidFirstHour ) == "undefined" )
    avoidFirstHour = ParamToggle( "Avoid First Hour", "No|Yes", 0 );
'''


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------


class TestParseParams:
    def test_typeof_guard_param(self, sample_include_afl):
        meta = parse_indicator_metadata(sample_include_afl, "tema.afl")
        assert len(meta.params) == 1
        p = meta.params[0]
        assert p.var_name == "sessionResetTime"
        assert p.default == "0"
        assert p.min_val == "0"
        assert p.max_val == "235959"
        assert p.step == "1"
        assert p.has_guard is True
        assert p.param_type == "Param"

    def test_isempty_guard_param(self, sample_isempty_afl):
        meta = parse_indicator_metadata(sample_isempty_afl, "range_bound.afl")
        assert len(meta.params) == 2
        var_names = [p.var_name for p in meta.params]
        assert "rangePeriod" in var_names
        assert "rangeThreshold" in var_names
        # Check defaults
        rp = next(p for p in meta.params if p.var_name == "rangePeriod")
        assert rp.default == "20"
        assert rp.has_guard is True

    def test_bare_param(self, sample_standalone_afl):
        meta = parse_indicator_metadata(sample_standalone_afl, "adx.afl")
        period = next(p for p in meta.params if p.var_name == "period")
        assert period.default == "14"
        assert period.min_val == "5"
        assert period.max_val == "50"
        assert period.has_guard is False

    def test_bare_param_toggle(self, sample_standalone_afl):
        meta = parse_indicator_metadata(sample_standalone_afl, "adx.afl")
        toggle = next(p for p in meta.params if p.param_type == "ParamToggle")
        assert toggle.var_name == "showDI"
        assert toggle.toggle_options == "No|Yes"
        assert toggle.default == "1"
        assert toggle.has_guard is False

    def test_typeof_guard_toggle(self, sample_typeof_toggle_afl):
        meta = parse_indicator_metadata(sample_typeof_toggle_afl, "session.afl")
        assert len(meta.params) == 1
        p = meta.params[0]
        assert p.var_name == "avoidFirstHour"
        assert p.param_type == "ParamToggle"
        assert p.toggle_options == "No|Yes"
        assert p.default == "0"
        assert p.has_guard is True


class TestParseInputsOutputs:
    def test_required_inputs(self, sample_include_afl):
        meta = parse_indicator_metadata(sample_include_afl, "tema.afl")
        required = [i for i in meta.required_inputs if not i.optional]
        optional = [i for i in meta.required_inputs if i.optional]
        assert len(required) == 2
        var_names = [i.var_name for i in required]
        assert "smoothingLength" in var_names
        assert "sourcePrice" in var_names
        assert len(optional) == 1
        assert optional[0].var_name == "sessionResetTime"

    def test_output_vars(self, sample_include_afl):
        meta = parse_indicator_metadata(sample_include_afl, "tema.afl")
        assert "temas" in meta.output_vars

    def test_multiple_outputs(self, sample_isempty_afl):
        meta = parse_indicator_metadata(sample_isempty_afl, "range_bound.afl")
        assert "isRangeBound" in meta.output_vars
        assert "rangeStrength" in meta.output_vars

    def test_no_inputs_or_outputs(self, sample_standalone_afl):
        meta = parse_indicator_metadata(sample_standalone_afl, "adx.afl")
        assert len(meta.required_inputs) == 0
        assert len(meta.output_vars) == 0


class TestParseMetadata:
    def test_indicator_type_include(self, sample_include_afl):
        meta = parse_indicator_metadata(sample_include_afl, "tema.afl")
        assert meta.indicator_type == "include"

    def test_indicator_type_standalone(self, sample_standalone_afl):
        meta = parse_indicator_metadata(sample_standalone_afl, "adx.afl")
        assert meta.indicator_type == "standalone"
        assert meta.has_plots is True

    def test_display_name_from_header(self, sample_include_afl):
        meta = parse_indicator_metadata(sample_include_afl, "tema.afl")
        assert "TEMA" in meta.display_name

    def test_display_name_from_filename(self):
        meta = parse_indicator_metadata("// just some code\n", "my_custom_indicator.afl")
        assert meta.display_name == "My Custom Indicator"

    def test_empty_content(self):
        meta = parse_indicator_metadata("", "empty.afl")
        assert meta.filename == "empty.afl"
        assert len(meta.params) == 0
        assert len(meta.required_inputs) == 0
        assert len(meta.output_vars) == 0

    def test_description_parsed(self, sample_include_afl):
        meta = parse_indicator_metadata(sample_include_afl, "tema.afl")
        assert "TEMA" in meta.description or "smooth" in meta.description.lower()


# ---------------------------------------------------------------------------
# Include block generation
# ---------------------------------------------------------------------------


class TestGenerateInclude:
    def test_single_indicator(self, ind_dir, sample_include_afl):
        (ind_dir / "tema.afl").write_text(sample_include_afl, encoding="utf-8")
        block = generate_include_block(
            [{"filename": "tema.afl", "params": {"smoothingLength": "14", "sourcePrice": "Close"}}],
            indicators_dir=ind_dir,
        )
        assert "#include_once" in block
        assert "tema.afl" in block
        assert "smoothingLength = 14;" in block
        assert "sourcePrice = Close;" in block

    def test_multiple_indicators(self, ind_dir, sample_include_afl, sample_isempty_afl):
        (ind_dir / "tema.afl").write_text(sample_include_afl, encoding="utf-8")
        (ind_dir / "range_bound.afl").write_text(sample_isempty_afl, encoding="utf-8")
        block = generate_include_block(
            [
                {"filename": "tema.afl", "params": {"smoothingLength": "14", "sourcePrice": "Close"}},
                {"filename": "range_bound.afl", "params": {"rangePeriod": "30"}},
            ],
            indicators_dir=ind_dir,
        )
        assert block.count("#include_once") == 2

    def test_absolute_path_in_include(self, ind_dir, sample_include_afl):
        (ind_dir / "tema.afl").write_text(sample_include_afl, encoding="utf-8")
        block = generate_include_block(
            [{"filename": "tema.afl", "params": {"smoothingLength": "14", "sourcePrice": "Close"}}],
            indicators_dir=ind_dir,
        )
        # Path should be absolute (contains drive letter or root)
        abs_path = str((ind_dir / "tema.afl").resolve())
        assert abs_path.replace("/", "\\") in block or abs_path in block

    def test_missing_indicator_raises(self, ind_dir):
        with pytest.raises(FileNotFoundError):
            generate_include_block(
                [{"filename": "nonexistent.afl", "params": {}}],
                indicators_dir=ind_dir,
            )

    def test_empty_indicators_list(self, ind_dir):
        block = generate_include_block([], indicators_dir=ind_dir)
        assert block == ""

    def test_header_and_footer_comments(self, ind_dir, sample_include_afl):
        (ind_dir / "tema.afl").write_text(sample_include_afl, encoding="utf-8")
        block = generate_include_block(
            [{"filename": "tema.afl", "params": {"smoothingLength": "14", "sourcePrice": "Close"}}],
            indicators_dir=ind_dir,
        )
        assert "auto-generated" in block.lower()
        assert "End Indicator Includes" in block


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_save_and_read(self, ind_dir):
        ok, msg = save_indicator("test.afl", "// test content\n", indicators_dir=ind_dir)
        assert ok is True
        content, meta = read_indicator("test.afl", indicators_dir=ind_dir)
        assert content == "// test content\n"
        assert meta is not None
        assert meta.filename == "test.afl"

    def test_delete(self, ind_dir):
        save_indicator("test.afl", "// test\n", indicators_dir=ind_dir)
        ok, msg = delete_indicator("test.afl", indicators_dir=ind_dir)
        assert ok is True
        content, meta = read_indicator("test.afl", indicators_dir=ind_dir)
        assert meta is None

    def test_delete_nonexistent(self, ind_dir):
        ok, msg = delete_indicator("nonexistent.afl", indicators_dir=ind_dir)
        assert ok is False

    def test_list(self, ind_dir):
        save_indicator("alpha.afl", "// indicator A\n", indicators_dir=ind_dir)
        save_indicator("beta.afl", "// indicator B\n", indicators_dir=ind_dir)
        indicators = list_indicators(indicators_dir=ind_dir)
        names = [ind.filename for ind in indicators]
        assert "alpha.afl" in names
        assert "beta.afl" in names

    def test_list_empty_dir(self, ind_dir):
        indicators = list_indicators(indicators_dir=ind_dir)
        assert indicators == []

    def test_path_traversal_blocked(self, ind_dir):
        ok, msg = save_indicator("../evil.afl", "// malicious\n", indicators_dir=ind_dir)
        assert ok is False
        assert "traversal" in msg.lower() or "invalid" in msg.lower()

    def test_auto_add_extension(self, ind_dir):
        ok, msg = save_indicator("myindicator", "// content\n", indicators_dir=ind_dir)
        assert ok is True
        content, meta = read_indicator("myindicator.afl", indicators_dir=ind_dir)
        assert meta is not None

    def test_read_nonexistent(self, ind_dir):
        content, meta = read_indicator("ghost.afl", indicators_dir=ind_dir)
        assert content == ""
        assert meta is None


# ---------------------------------------------------------------------------
# Real indicator files (if available)
# ---------------------------------------------------------------------------


class TestRealIndicators:
    """Test against the actual indicator files in the user's directory."""

    _indicators_path = Path(__file__).resolve().parent.parent / "indicators"

    def test_parse_consolidation_zones(self):
        path = self._indicators_path / "consolidation_zones.afl"
        if not path.exists():
            pytest.skip("consolidation_zones.afl not available")
        content = path.read_text(encoding="utf-8")
        meta = parse_indicator_metadata(content, "consolidation_zones.afl")
        assert len(meta.params) >= 7
        assert meta.indicator_type == "include"
        # All params should have typeof guards
        for p in meta.params:
            assert p.has_guard is True

    def test_parse_tema(self):
        path = self._indicators_path / "tema.afl"
        if not path.exists():
            pytest.skip("tema.afl not available")
        content = path.read_text(encoding="utf-8")
        meta = parse_indicator_metadata(content, "tema.afl")
        assert len(meta.params) >= 1
        assert meta.indicator_type == "include"
        assert "temas" in meta.output_vars
        required_names = [i.var_name for i in meta.required_inputs if not i.optional]
        assert "smoothingLength" in required_names
        assert "sourcePrice" in required_names

    def test_parse_range_bound(self):
        path = self._indicators_path / "range_bound.afl"
        if not path.exists():
            pytest.skip("range_bound.afl not available")
        content = path.read_text(encoding="utf-8")
        meta = parse_indicator_metadata(content, "range_bound.afl")
        assert len(meta.params) >= 2
        assert meta.indicator_type == "include"

    def test_parse_adx_standalone(self):
        path = self._indicators_path / "adx.afl"
        if not path.exists():
            pytest.skip("adx.afl not available")
        content = path.read_text(encoding="utf-8")
        meta = parse_indicator_metadata(content, "adx.afl")
        assert meta.indicator_type == "standalone"
        assert meta.has_plots is True

    def test_parse_vwap_clouds(self):
        path = self._indicators_path / "vwap_clouds.afl"
        if not path.exists():
            pytest.skip("vwap_clouds.afl not available")
        content = path.read_text(encoding="utf-8")
        meta = parse_indicator_metadata(content, "vwap_clouds.afl")
        assert meta.indicator_type == "include"
        assert "VWAP" in meta.output_vars
