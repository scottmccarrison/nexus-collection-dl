"""Hostile-input matrix for the nxm:// link parser.

Covers the strict shape rules (scheme, authority, path, query), credential
pairing and ambiguity rejection, percent-decoding exactly once, collection
link recognition with safe guidance, expiry checks, and redaction: no error
message or repr may ever carry the raw URI, the key, or the expires value.
"""

import time

import pytest

from nexus_collection_dl.nxm import (
    MAX_NXM_URI_LENGTH,
    NxmCollectionLinkError,
    NxmLinkInfo,
    NxmParseError,
    NxmSchemeError,
    parse_nxm_url,
)

GAME = "skyrimspecialedition"
KEY = "Tok3nValue-abc_XYZ.123"
EXPIRES = 1893456000  # 2030-01-01, comfortably in the future
USER_ID = 4242

VALID_FREE = f"nxm://{GAME}/mods/123/files/456?key={KEY}&expires={EXPIRES}&user_id={USER_ID}"
VALID_PREMIUM = f"nxm://{GAME}/mods/123/files/456"


# ---------------------------------------------------------------------------
# Valid links
# ---------------------------------------------------------------------------

def test_valid_premium_link():
    info = parse_nxm_url(VALID_PREMIUM)
    assert info.game_domain == GAME
    assert info.mod_id == 123
    assert info.file_id == 456
    assert info.key is None and info.expires is None and info.user_id is None
    assert not info.has_credentials


def test_valid_free_link_with_credentials():
    info = parse_nxm_url(VALID_FREE)
    assert info.key == KEY
    assert info.expires == EXPIRES
    assert info.user_id == USER_ID
    assert info.has_credentials


def test_scheme_case_insensitive():
    info = parse_nxm_url(VALID_PREMIUM.replace("nxm://", "NXM://"))
    assert info.mod_id == 123
    info = parse_nxm_url(VALID_PREMIUM.replace("nxm://", "NxM://"))
    assert info.file_id == 456


def test_game_domain_normalized_and_shapes_allowed():
    assert parse_nxm_url("nxm://BaldursGate3/mods/1/files/2").game_domain == "baldursgate3"
    assert parse_nxm_url("nxm://some-game/mods/1/files/2").game_domain == "some-game"


def test_harmless_extra_params_ignored():
    info = parse_nxm_url(
        f"nxm://{GAME}/mods/123/files/456?key={KEY}&expires={EXPIRES}"
        "&utm_source=site&campaign=a%20b&flag=1"
    )
    assert info.key == KEY
    assert info.expires == EXPIRES


def test_key_decoded_exactly_once():
    # %2B -> '+', %3D -> '='; a double-encoded %252B decodes to '%2B' text,
    # never all the way to '+'.
    info = parse_nxm_url(f"nxm://{GAME}/mods/1/files/2?key=ab%2Bcd%3D&expires={EXPIRES}")
    assert info.key == "ab+cd="
    info = parse_nxm_url(f"nxm://{GAME}/mods/1/files/2?key=ab%252Bcd&expires={EXPIRES}")
    assert info.key == "ab%2Bcd"


def test_user_id_only_is_valid_premium_shape():
    info = parse_nxm_url(f"nxm://{GAME}/mods/1/files/2?user_id={USER_ID}")
    assert info.user_id == USER_ID
    assert not info.has_credentials


# ---------------------------------------------------------------------------
# Rejected shapes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uri", [
    "",                                             # empty
    "https://www.nexusmods.com/skyrim/mods/1",      # wrong scheme
    "nxm:/skyrimspecialedition/mods/1/files/2",     # malformed authority
    f"nxm://{GAME}/mods/123/files/456?" + "a=b&" * 2500,  # oversized (>8192)
])
def test_reject_bad_scheme_or_size(uri):
    with pytest.raises(NxmParseError):
        parse_nxm_url(uri)


def test_oversize_link_is_rejected_for_length():
    uri = f"nxm://{GAME}/mods/1/files/2?" + "a=b&" * 2500
    assert len(uri) > MAX_NXM_URI_LENGTH
    with pytest.raises(NxmParseError, match="too long"):
        parse_nxm_url(uri)


def test_reject_non_string():
    with pytest.raises(NxmParseError):
        parse_nxm_url(None)


# ---------------------------------------------------------------------------
# Non-nxm inputs: wrong link type gets actionable guidance, never an echo
# ---------------------------------------------------------------------------

def test_non_nxm_scheme_error_explains_link_type():
    url = "https://www.nexusmods.com/skyrim/mods/1?tab=files&tok=SUPERSECRET123"
    with pytest.raises(NxmSchemeError) as excinfo:
        parse_nxm_url(url)
    err = excinfo.value
    assert err.scheme == "https"
    message = str(err)
    assert "not an nxm:// link" in message
    assert "not usable" in message
    assert "Mod manager download" in message   # where nxm:// links come from
    assert "copy link address" in message      # right-click copy
    assert "handler install" in message        # click-through registration
    # The raw URI is never echoed: no host, path, or query values.
    assert url not in message
    assert "nexusmods.com" not in message
    assert "SUPERSECRET123" not in message
    assert "tab=files" not in message


def test_non_nxm_scheme_variants():
    with pytest.raises(NxmSchemeError) as excinfo:
        parse_nxm_url("http://example.com/x")
    assert excinfo.value.scheme == "http"
    with pytest.raises(NxmSchemeError) as excinfo:
        parse_nxm_url("HTTPS://example.com/x")  # token normalized to lowercase
    assert excinfo.value.scheme == "https"
    with pytest.raises(NxmSchemeError) as excinfo:
        parse_nxm_url("ftp://example.invalid/mod.zip")
    assert excinfo.value.scheme == "ftp"
    assert "example.invalid" not in str(excinfo.value)


def test_malformed_nxm_forms_are_not_scheme_errors():
    # A broken nxm shape keeps the generic parse error, never the
    # wrong-link-type guidance (which would claim a different scheme).
    with pytest.raises(NxmParseError) as excinfo:
        parse_nxm_url("nxm:/skyrimspecialedition/mods/1/files/2")
    assert not isinstance(excinfo.value, NxmSchemeError)
    with pytest.raises(NxmParseError) as excinfo:
        parse_nxm_url("not-a-link-at-all")
    assert not isinstance(excinfo.value, NxmSchemeError)


def test_non_nxm_scheme_wins_over_percent_validation():
    # An https URL with malformed percent escapes still gets the
    # wrong-link-type guidance (scheme detection runs first), and the
    # hostile query value is never echoed.
    with pytest.raises(NxmSchemeError) as excinfo:
        parse_nxm_url("https://example.com/a%zz?key=SUPERSECRET123")
    assert "SUPERSECRET123" not in str(excinfo.value)
    assert "example.com" not in str(excinfo.value)


@pytest.mark.parametrize("bad", [
    f"nxm://{GAME}/mods/1/files/2\n",              # newline
    f"nxm://{GAME}/mods/1\x00/files/2",            # NUL
    f"nxm://{GAME}/mods/1/files/2?key=a\x1bb",     # escape char
    f"nxm://{GAME}/mods/1 /files/2",               # space
    f"nxm://{GAME}\t/mods/1/files/2",              # tab
    f"nxm://{GAME}/mods/1/files/2\x7f",            # DEL
])
def test_reject_control_and_whitespace(bad):
    with pytest.raises(NxmParseError):
        parse_nxm_url(bad)


@pytest.mark.parametrize("bad", [
    f"nxm://{GAME}/mods/1%zz/files/2",             # invalid hex
    f"nxm://{GAME}/mods/1/files/2?key=ab%",        # trailing percent
    f"nxm://{GAME}/mods/1/files/2?key=%2",         # truncated escape
    f"nxm://{GAME}%2G/mods/1/files/2",             # invalid hex in domain
])
def test_reject_invalid_percent_encoding(bad):
    with pytest.raises(NxmParseError):
        parse_nxm_url(bad)


@pytest.mark.parametrize("bad", [
    f"nxm://user:pass@{GAME}/mods/1/files/2",      # userinfo with port-like colon
    f"nxm://user@{GAME}/mods/1/files/2",           # userinfo
    f"nxm://{GAME}:8080/mods/1/files/2",           # port
    f"nxm://{GAME}/mods/1/files/2#fragment",       # fragment
    f"nxm://{GAME}/mods/1/files/2?key=a#b",        # fragment after query
])
def test_reject_authority_and_fragment_abuse(bad):
    with pytest.raises(NxmParseError):
        parse_nxm_url(bad)


@pytest.mark.parametrize("bad", [
    "nxm:///mods/1/files/2",                       # empty game domain
    f"nxm://{GAME}_x/mods/1/files/2",              # underscore in domain
    f"nxm://{GAME}.evil/mods/1/files/2",           # dot in domain
    f"nxm://-{GAME}/mods/1/files/2",               # leading hyphen
    f"nxm://{GAME}%20x/mods/1/files/2",            # encoded space in domain
])
def test_reject_bad_game_domain(bad):
    with pytest.raises(NxmParseError):
        parse_nxm_url(bad)


@pytest.mark.parametrize("bad", [
    f"nxm://{GAME}",                               # no path
    f"nxm://{GAME}/",                              # empty path
    f"nxm://{GAME}/mods/1",                        # missing files segment
    f"nxm://{GAME}/mods/1/files",                  # missing file id
    f"nxm://{GAME}/files/2/mods/1",                # wrong order
    f"nxm://{GAME}/mods/1/files/2/extra",          # extra segment
    f"nxm://{GAME}/mods/1/files/2/",               # trailing slash
    f"nxm://{GAME}/mods/1/files/2/../../etc/passwd",  # traversal
    f"nxm://{GAME}/mods/../1/files/2",             # traversal in mod segment
    f"nxm://{GAME}/mods/%2e%2e/files/2",           # encoded traversal
    f"nxm://{GAME}\\mods\\1\\files\\2",            # backslashes
    f"nxm://{GAME}/mods/0/files/2",                # zero mod id
    f"nxm://{GAME}/mods/1/files/0",                # zero file id
    f"nxm://{GAME}/mods/-1/files/2",               # negative mod id
    f"nxm://{GAME}/mods/007/files/2",              # leading zeros
    f"nxm://{GAME}/mods/abc/files/2",              # non-numeric
    f"nxm://{GAME}/mods/1234567890123/files/2",    # 13-digit mod id
])
def test_reject_bad_paths(bad):
    with pytest.raises(NxmParseError):
        parse_nxm_url(bad)


@pytest.mark.parametrize("bad", [
    f"nxm://{GAME}/mods/1/files/2?key={KEY}",                 # key without expires
    f"nxm://{GAME}/mods/1/files/2?expires={EXPIRES}",         # expires without key
    f"nxm://{GAME}/mods/1/files/2?key=a&key=b&expires={EXPIRES}",   # duplicate key
    f"nxm://{GAME}/mods/1/files/2?key={KEY}&expires=1&expires=2",   # duplicate expires
    f"nxm://{GAME}/mods/1/files/2?user_id=1&user_id=2",       # duplicate user_id
    f"nxm://{GAME}/mods/1/files/2?KEY=a&key=b&expires={EXPIRES}",   # case-variant dupe
    f"nxm://{GAME}/mods/1/files/2?%6bey=a&key=b&expires={EXPIRES}",  # encoded-name dupe
])
def test_reject_credential_ambiguity(bad):
    with pytest.raises(NxmParseError):
        parse_nxm_url(bad)


@pytest.mark.parametrize("bad", [
    f"nxm://{GAME}/mods/1/files/2?key=&expires={EXPIRES}",    # empty key
    f"nxm://{GAME}/mods/1/files/2?key=ab%20cd&expires={EXPIRES}",  # space in key
    f"nxm://{GAME}/mods/1/files/2?key={'A' * 513}&expires={EXPIRES}",  # oversized key
    f"nxm://{GAME}/mods/1/files/2?key={KEY}&expires=0",       # zero expires
    f"nxm://{GAME}/mods/1/files/2?key={KEY}&expires=-5",      # negative expires
    f"nxm://{GAME}/mods/1/files/2?key={KEY}&expires=abc",     # non-numeric expires
    f"nxm://{GAME}/mods/1/files/2?key={KEY}&expires=1.5",     # float expires
    f"nxm://{GAME}/mods/1/files/2?key={KEY}&expires=1234567890123",  # 13-digit expires
    f"nxm://{GAME}/mods/1/files/2?user_id=0",                 # zero user_id
    f"nxm://{GAME}/mods/1/files/2?user_id=abc",               # non-numeric user_id
])
def test_reject_bad_credential_values(bad):
    with pytest.raises(NxmParseError):
        parse_nxm_url(bad)


@pytest.mark.parametrize("bad", [
    f"nxm://{GAME}/mods/1/files/2?&&key={KEY}&expires={EXPIRES}",  # empty segment
    f"nxm://{GAME}/mods/1/files/2?key={KEY}&expires={EXPIRES}&",   # trailing &
    f"nxm://{GAME}/mods/1/files/2?flag",                           # no '='
])
def test_reject_malformed_query(bad):
    with pytest.raises(NxmParseError):
        parse_nxm_url(bad)


# ---------------------------------------------------------------------------
# Collection links: recognized, redirected to sync, never executed
# ---------------------------------------------------------------------------

def test_collection_link_raises_with_safe_guidance():
    uri = f"nxm://{GAME}/collections/cool-mods/revisions/7?key={KEY}&expires={EXPIRES}"
    with pytest.raises(NxmCollectionLinkError) as excinfo:
        parse_nxm_url(uri)
    err = excinfo.value
    assert err.slug == "cool-mods"
    assert err.revision == "7"
    assert err.web_url == f"https://next.nexusmods.com/{GAME}/collections/cool-mods"
    message = str(err)
    assert "nexus-dl sync" in message
    assert err.web_url in message
    # The raw nxm URI and its credentials never appear in guidance (the
    # generic scheme name may appear in prose).
    assert uri not in message
    assert f"nxm://{GAME}/collections" not in message
    assert KEY not in message
    assert str(EXPIRES) not in message


def test_collection_link_latest_and_bare_shapes():
    with pytest.raises(NxmCollectionLinkError) as excinfo:
        parse_nxm_url(f"nxm://{GAME}/collections/abc/revisions/latest")
    assert excinfo.value.revision == "latest"
    with pytest.raises(NxmCollectionLinkError) as excinfo:
        parse_nxm_url(f"nxm://{GAME}/collections/abc")
    assert excinfo.value.revision == "latest"


def test_collection_link_bad_slug_is_plain_parse_error():
    with pytest.raises(NxmParseError) as excinfo:
        parse_nxm_url(f"nxm://{GAME}/collections/..%2f..%2fevil/revisions/1")
    assert not isinstance(excinfo.value, NxmCollectionLinkError)
    with pytest.raises(NxmParseError):
        parse_nxm_url(f"nxm://{GAME}/collections/ok/revisions/1/extra")


# ---------------------------------------------------------------------------
# Redaction and expiry
# ---------------------------------------------------------------------------

def test_repr_and_str_hide_credentials():
    info = parse_nxm_url(VALID_FREE)
    for text in (repr(info), str(info)):
        assert KEY not in text
        assert str(EXPIRES) not in text
        assert "[redacted]" in text
        assert GAME in text and "123" in text and "456" in text
    assert info.redacted_uri() == f"nxm://{GAME}/mods/123/files/456"


def test_parse_errors_never_echo_uri_or_secrets():
    hostile = (
        f"nxm://{GAME}/mods/1/files/2/extra?user@host:key=SUPERSECRET123"
        "&expires=1700000001&dup=SUPERSECRET123"
    )
    with pytest.raises(NxmParseError) as excinfo:
        parse_nxm_url(hostile)
    message = str(excinfo.value)
    assert "SUPERSECRET123" not in message
    assert "1700000001" not in message
    assert hostile not in message
    assert repr(excinfo.value).count("SUPERSECRET123") == 0


def test_expiry_check_is_local():
    past = int(time.time()) - 60
    future = int(time.time()) + 3600
    assert parse_nxm_url(f"nxm://{GAME}/mods/1/files/2?key={KEY}&expires={past}").is_expired()
    assert not parse_nxm_url(
        f"nxm://{GAME}/mods/1/files/2?key={KEY}&expires={future}"
    ).is_expired()
    assert not parse_nxm_url(VALID_PREMIUM).is_expired()
    # Explicit now: deterministic boundary behavior
    info = parse_nxm_url(f"nxm://{GAME}/mods/1/files/2?key={KEY}&expires=1000")
    assert info.is_expired(now=1000)      # expires <= now is expired
    assert not info.is_expired(now=999)


def test_info_dataclass_defaults_are_credential_free():
    info = NxmLinkInfo(game_domain=GAME, mod_id=1, file_id=2)
    assert "None" in repr(info)
    assert not info.has_credentials
    assert not info.is_expired()
