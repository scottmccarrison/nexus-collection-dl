"""Decoding ModuleConfig.xml bytes in whatever encoding the author used.

Real configs ship as UTF-16LE with a BOM, but BOM-less UTF-16 appears too.
Two things then matter: NUL-interleaved bytes must not be decoded as
UTF-8 (NULs are valid UTF-8, so the decode "succeeds" as NUL-riddled
text), and a decode that parses to nothing must fall through to the next
encoding instead of giving up.
"""

from __future__ import annotations

from nexus_collection_dl.fomod_plan import (
    decode_config_text,
    load_module_config_bytes,
    load_module_config_text,
)

CONFIG_XML = """<?xml version="1.0" encoding="UTF-16"?>
<config>
  <moduleName>Encoded</moduleName>
  <installSteps>
    <installStep name="Options">
      <optionalFileGroups>
        <group name="Pack" type="SelectExactlyOne">
          <plugins>
            <plugin name="Alpha">
              <description>The alpha pack.</description>
              <files>
                <file source="alpha/manifest.json" destination="manifest.json" priority="0"/>
              </files>
              <typeDescriptor>
                <type name="Optional"/>
              </typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>
"""

NON_ASCII_XML = CONFIG_XML.replace("The alpha pack.", "Le paquet déscription.")


def test_bomless_utf16le_config_parses():
    """A BOM-less UTF-16LE config must not read as unreadable."""
    raw = CONFIG_XML.encode("utf-16-le")
    config = load_module_config_bytes(raw)
    assert config is not None
    assert [s.name for s in config.steps] == ["Options"]


def test_bomless_utf16be_config_parses():
    """Byte-swapped BOM-less UTF-16 is real too."""
    raw = CONFIG_XML.encode("utf-16-be")
    config = load_module_config_bytes(raw)
    assert config is not None
    assert [s.name for s in config.steps] == ["Options"]


def test_bomless_utf16be_with_non_ascii_content_parses():
    """A mis-decode must fall through, not end the search.

    ASCII-only BOM-less UTF-16 happens to survive a UTF-8 round trip;
    content with real characters does not, and the first codec's
    mis-decode used to be the end of it.
    """
    raw = NON_ASCII_XML.encode("utf-16-be")
    config = load_module_config_bytes(raw)
    assert config is not None
    assert [s.name for s in config.steps] == ["Options"]


def test_decode_config_text_returns_clean_text_for_bomless_utf16():
    """The stored copy of a config must not be NUL-garbage.

    ``_fomod_config_record`` keeps this text in the state file; the
    decode must pick the codec the bytes were written in.
    """
    for raw in (CONFIG_XML.encode("utf-16-le"), CONFIG_XML.encode("utf-16-be")):
        text = decode_config_text(raw)
        assert text is not None
        assert "\x00" not in text
        assert text == CONFIG_XML
        stored = load_module_config_text(text)
        assert stored is not None
        assert [s.name for s in stored.steps] == ["Options"]


def test_bom_utf16_and_utf8_configs_still_parse():
    """The common cases must keep working through the byte path."""
    for raw in (CONFIG_XML.encode("utf-16"), CONFIG_XML.encode("utf-8")):
        config = load_module_config_bytes(raw)
        assert config is not None
        assert [s.name for s in config.steps] == ["Options"]


PLAIN_XML = """<config>
  <moduleName>Plain</moduleName>
  <installSteps><installStep name="Options"><optionalFileGroups>
    <group name="Pack" type="SelectExactlyOne"><plugins>
      <plugin name="Alpha">
        <description>Le café.</description>
        <files><file source="a/t.esp" destination="t.esp"/></files>
        <typeDescriptor><type name="Optional"/></typeDescriptor>
      </plugin>
    </plugins></group>
  </optionalFileGroups></installStep></installSteps>
</config>"""


def test_cp1252_text_decodes_as_itself_not_as_utf16_mojibake():
    """A decode that succeeds can still be the wrong codec.

    Even-length NUL-free bytes decode as UTF-16 without error, so a
    cp1252 config with a non-ASCII byte used to be stored in the state
    record as CJK mojibake nothing could read back.
    """
    text = PLAIN_XML
    if len(text.encode("cp1252")) % 2:
        text += " "
    raw = text.encode("cp1252")
    assert len(raw) % 2 == 0
    assert decode_config_text(raw) == text
    config = load_module_config_bytes(raw)
    assert config is not None
    assert [s.name for s in config.steps] == ["Options"]


def test_pathologically_nested_config_reads_as_unreadable():
    """Deep dependency nesting must not take the process down."""
    nested = "<dependencies>" * 500 + "</dependencies>" * 500
    xml = (
        '<?xml version="1.0"?>\n<config>\n'
        "  <moduleName>N</moduleName>\n"
        "  <installSteps><installStep name=\"S\"/></installSteps>\n"
        f"  <conditionalFileInstalls><patterns><pattern>{nested}"
        "<files><file source=\"a\" destination=\"a\"/></files>"
        "</pattern></patterns></conditionalFileInstalls>\n</config>"
    ).encode()
    assert load_module_config_bytes(xml) is None
