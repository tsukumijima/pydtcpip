"""Verify that device profile changes stay within their own families."""

import ast
from pathlib import Path

import pytest

from pydtcpip.dlna.description import parse_device_description
from pydtcpip.dlna.didl import parse_didl
from pydtcpip.dlna.profiles import GENERIC, PROFILES, select_profile
from pydtcpip.dlna.profiles.nasne import NASNE
from pydtcpip.dlna.profiles.panasonic import PANASONIC
from pydtcpip.errors import ClientNotAllowedError
from pydtcpip.models import Recording


_PACKAGE_DIRECTORY = Path(__file__).parent.parent / 'pydtcpip'
_PROFILE_DIRECTORY = _PACKAGE_DIRECTORY / 'dlna' / 'profiles'
_FIXTURE_DIRECTORY = Path(__file__).parent / 'fixtures'

# Modules profiles may import (public types and parent profiles)
_PROFILE_ALLOWED_IMPORTS = frozenset(
    (
        'pydtcpip.dlna.description',
        'pydtcpip.errors',
        'pydtcpip.models',
    )
)

# Expected profile, copy count status, copy count value, and DLNA.ORG_CI of the original resource
## None in the final column means the fixture contains no eligible original resource
_EXPECTED = {
    # A regza:copycount of 10 includes the final Move, leaving 9 copies as shown on the TV
    ('toshiba', 'dbr-z620'): ('toshiba', 'known', 9, '0'),
    ('toshiba_regza_tv', '43z570l'): ('toshiba_regza_tv', 'omitted', None, '0'),
    # Every resource in this fixture has CI=1 in its URL, including the one with CopyCount
    ('panasonic', 'dmr-brx2060'): ('panasonic', 'omitted', None, None),
}


def _fixture_devices() -> list[tuple[str, str]]:
    """
    Find device fixtures containing a description.

    Returns:
        list[tuple[str, str]]: Family and model directory names
    """

    devices: list[tuple[str, str]] = []
    for description in sorted(_FIXTURE_DIRECTORY.glob('*/*/description.xml')):
        devices.append((description.parent.parent.name, description.parent.name))
    return devices


def _imported_modules(path: Path) -> list[str]:
    """
    Collect modules imported by a Python file.

    Args:
        path (Path): Python source file

    Returns:
        list[str]: Imported module names
    """

    tree = ast.parse(path.read_text(encoding='utf-8'))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
    return modules


def test_profiles_import_only_public_types_and_parent_profiles() -> None:
    # Profiles importing core internals could affect other devices
    for path in sorted(_PROFILE_DIRECTORY.glob('*.py')):
        if path.name == '__init__.py':
            continue
        for module in _imported_modules(path):
            if module.startswith('pydtcpip.') is False:
                continue
            is_allowed = module in _PROFILE_ALLOWED_IMPORTS or module.startswith('pydtcpip.dlna.profiles.')
            assert is_allowed is True, f'{path.name} imports {module}'


def test_core_imports_profiles_only_through_registry() -> None:
    # Direct imports allow family-specific branches to creep into the core
    for path in sorted(_PACKAGE_DIRECTORY.rglob('*.py')):
        if path.is_relative_to(_PROFILE_DIRECTORY) is True:
            continue
        for module in _imported_modules(path):
            if module.startswith('pydtcpip.dlna.profiles.') is True:
                pytest.fail(f'{path.relative_to(_PACKAGE_DIRECTORY)} imports {module} directly')


def test_every_profile_is_registered_and_named_after_its_file() -> None:
    # Unregistered profiles cannot be selected, and mismatched names make profiles hard to find
    registered_names = {profile.name for profile in PROFILES}
    for path in sorted(_PROFILE_DIRECTORY.glob('*.py')):
        if path.name in ('__init__.py', 'base.py'):
            continue
        assert path.stem in registered_names, f'{path.stem} is not registered in PROFILES'


@pytest.mark.parametrize(('vendor', 'model'), _fixture_devices())
def test_each_fixture_matches_exactly_one_deepest_profile(vendor: str, model: str) -> None:
    # Two matches at the same depth could make a new profile take over an existing device
    raw_xml = (_FIXTURE_DIRECTORY / vendor / model / 'description.xml').read_text(encoding='utf-8')
    description = parse_device_description('http://192.0.2.1/description.xml', raw_xml)
    matched = [profile for profile in PROFILES if profile.is_match(description) is True]
    deepest_depth = max(profile.depth for profile in matched)
    deepest = [profile.name for profile in matched if profile.depth == deepest_depth]
    assert len(deepest) == 1, f'{vendor}/{model} matches multiple profiles at the same depth: {deepest}'
    assert deepest[0] == vendor


@pytest.mark.parametrize(('vendor', 'model'), _fixture_devices())
def test_fixture_recordings_keep_their_protection_values(vendor: str, model: str) -> None:
    # Detect changes to values parsed from saved device responses
    device_directory = _FIXTURE_DIRECTORY / vendor / model
    description = parse_device_description(
        'http://192.0.2.1/description.xml', (device_directory / 'description.xml').read_text(encoding='utf-8')
    )
    profile = select_profile(description)
    objects = parse_didl((device_directory / 'recording.xml').read_text(encoding='utf-8'), profile)
    recordings = [item for item in objects if isinstance(item, Recording)]
    assert len(recordings) == 1

    expected_profile, expected_status, expected_value, expected_ci = _EXPECTED[(vendor, model)]
    assert profile.name == expected_profile
    assert recordings[0].protection.remaining_copy_count.status == expected_status
    assert recordings[0].protection.remaining_copy_count.value == expected_value

    # Prefer the original resource (DLNA.ORG_CI=0) over a transcoded resource
    original = recordings[0].original_resource
    if expected_ci is None:
        assert original is None
        assert all(resource.is_converted is True for resource in recordings[0].resources)
        assert recordings[0].protection.reported_copy_counts[0].raw_value == '10'
        assert recordings[0].protection.reported_copy_counts[0].remaining_copies.value == 9
    else:
        assert original is not None
        assert original.protocol_info.parameters.get('DLNA.ORG_CI') == expected_ci


def test_generic_profile_handles_standard_dlna_without_vendor_meaning() -> None:
    # Detect changes to standard behavior for devices that match no family
    description = parse_device_description(
        'http://192.0.2.1/description.xml',
        '<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        '<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType><friendlyName>標準の機器</friendlyName>'
        '<manufacturer>Example</manufacturer><modelName>Example Server</modelName><UDN>uuid:00000000-0000-0000-0000-000000000001</UDN>'
        '</device></root>',
    )
    profile = select_profile(description)
    assert profile is GENERIC

    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<container id="1" parentID="0" childCount="2"><dc:title>録画</dc:title><upnp:class>object.container</upnp:class></container>'
        '<item id="2" parentID="1"><dc:title>番組</dc:title><upnp:class>object.item.videoItem</upnp:class>'
        '<upnp:genre>ニュース</upnp:genre><upnp:genre>報道</upnp:genre>'
        '<res protocolInfo="http-get:*:application/x-dtcp1;DTCP1HOST=192.0.2.1;DTCP1PORT=5000:DLNA.ORG_CI=0;DTCP.COM_FLAGS=C0000000">http://192.0.2.1/2</res>'
        '</item></DIDL-Lite>'
    )
    objects = parse_didl(didl, profile)
    assert [type(item).__name__ for item in objects] == ['Container', 'Recording']
    recording = objects[1]
    assert isinstance(recording, Recording)
    assert recording.genres == ('ニュース', '報道')
    assert recording.protection.remaining_copy_count.status == 'unsupported'
    assert recording.protection.is_move_supported.value is True
    assert recording.protection.is_transaction_move_supported.value is True
    assert profile.resolve_http_status()(403, 'http://192.0.2.1/2', None) is None


@pytest.mark.parametrize(
    ('manufacturer', 'model_name', 'expected_profile'),
    [
        ('Sony Computer Entertainment Inc.', 'nasne', 'nasne'),
        ('Buffalo Inc.', 'buffalo-nasne', 'nasne'),
        ('Buffalo Inc.', 'buffalo-nasne2', 'nasne'),
        ('Sony Corporation', 'BDZ-ZW1500', 'sony_bdz'),
        ('Sharp Corporation', 'BD-W2700', 'sharp'),
        ('Panasonic', 'BD/DVD Recorder', 'panasonic'),
        ('I-O DATA DEVICE, INC.', 'GV-NTX2', 'recon'),
        ('I-O DATA DEVICE, INC.', 'RECBOX', 'recbox'),
        ('Buffalo Inc.', 'LinkStation', 'linkstation'),
        ('HUMAX', 'IS1050', 'stb'),
        ('Panasonic', 'STW2000', 'stb_stw2000'),
        ('Mitsubishi Electric Corporation', 'AM900', 'stb_am900'),
        ('HUAWEI', 'TT01', 'stb_hikaritv'),
        ('Example', 'TT01', 'generic'),
        ('TVS REGZA', '43M550M', 'toshiba_regza_tv'),
    ],
)
def test_nasne_model_names_select_a_unique_profile(manufacturer: str, model_name: str, expected_profile: str) -> None:
    """Select a unique family for each supported signature and retain generic negative controls."""

    description = parse_device_description(
        'http://192.0.2.1/description.xml',
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        '<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>'
        f'<manufacturer>{manufacturer}</manufacturer><modelName>{model_name}</modelName>'
        '<UDN>uuid:00000000-0000-0000-0000-000000000001</UDN></device></root>',
    )
    profile = select_profile(description)
    assert profile.name == expected_profile
    deepest_matches = []
    for candidate in PROFILES:
        if candidate.depth == profile.depth and candidate.is_match(description) is True:
            deepest_matches.append(candidate.name)
    assert deepest_matches == [expected_profile]


def test_regza_recorder_is_not_given_the_tv_listing_policy() -> None:
    """Recognize a recorder whose manufacturer uses the REGZA company name."""

    description = parse_device_description(
        'http://192.0.2.1/description.xml',
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        '<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>'
        '<manufacturer>TOSHIBA TVS REGZA</manufacturer><modelDescription>TOSHIBA RECORDER</modelDescription>'
        '<modelName>DBR-4KZ00</modelName></device></root>',
    )

    profile = select_profile(description)
    assert profile.name == 'toshiba'
    assert profile.resolve_has_listing_only_metadata() is False


def test_nasne_reports_client_registration_refusal() -> None:
    # Distinguish nasne's 403 from authentication failures so users can find client registration
    error = NASNE.resolve_http_status()(403, 'http://192.0.2.1/2', None)
    assert isinstance(error, ClientNotAllowedError)
    assert 'Client Device Management' in str(error)
    assert NASNE.resolve_http_status()(404, 'http://192.0.2.1/2', None) is None


def test_panasonic_reports_client_authorization_refusal() -> None:
    # Some recorders return UPnP 720 on a protected folder's children until the PC is authorized
    error = PANASONIC.resolve_http_status()(500, 'http://192.0.2.1/cds', 720)
    assert isinstance(error, ClientNotAllowedError)
    assert error.upnp_error_code == 720
    # Other UPnP faults under the same HTTP status keep their generic meaning
    assert PANASONIC.resolve_http_status()(500, 'http://192.0.2.1/cds', 709) is None
    assert PANASONIC.resolve_http_status()(500, 'http://192.0.2.1/cds', None) is None


@pytest.mark.parametrize(
    ('manufacturer', 'model_name', 'model_number', 'delay'),
    [
        ('Panasonic', 'BD/DVD Recorder', 'DMR-BRW1060', 5.0),
        ('Panasonic', 'DMR-BRW1060', '', 5.0),
        ('Panasonic', 'BD/DVD Recorder', 'DMR-BCW1060', 0.0),
        ('Panasonic', 'BD/DVD Recorder', 'DMR-BRX/BCX2060', 0.0),
        ('Panasonic', 'TV-43W90B', '', 0.0),
        ('Panasonic', 'TZ-WR500P', '', 0.0),
        ('Example', 'BD/DVD Recorder', 'DMR-BRW1060', 0.0),
    ],
)
def test_move_finalize_delay_targets_dmr_brw1060(
    manufacturer: str, model_name: str, model_number: str, delay: float
) -> None:
    """Keep the wait limited to devices that identify themselves as Panasonic DMR-BRW1060."""

    description = parse_device_description(
        'http://192.0.2.1/description.xml',
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        f'<manufacturer>{manufacturer}</manufacturer><modelName>{model_name}</modelName>'
        f'<modelNumber>{model_number}</modelNumber></device></root>',
    )
    assert select_profile(description).resolve_move_finalize_delay(description) == delay


def test_recording_keeps_all_long_descriptions_in_document_order() -> None:
    """Keep repeated ARIB and UPnP descriptions, including normalized broadcast marks."""

    objects = parse_didl(
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:arib="urn:schemas-arib-or-jp:elements-1-0/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="recording" parentID="0">'
        '<arib:longDescription>Summary\ue0fe</arib:longDescription>'
        '<upnp:longDescription>Cast</upnp:longDescription>'
        '<arib:longDescription />'
        '<arib:longDescription>More details</arib:longDescription>'
        '</item><item id="no-description" parentID="0" /></DIDL-Lite>',
        GENERIC,
    )
    recording, undescribed = objects
    assert isinstance(recording, Recording)
    assert isinstance(undescribed, Recording)
    assert recording.long_description == 'Summary🈑\nCast\nMore details'
    assert undescribed.long_description is None


@pytest.mark.parametrize(
    ('count', 'expected_status', 'expected_value'),
    [
        ('10', 'known', 9),
        ('9', 'known', 8),
        ('1', 'known', 0),
        (None, 'omitted', None),
        ('0', 'unsupported', None),
        ('redacted', 'unsupported', None),
    ],
)
def test_nasne_copy_count_excludes_the_final_move(
    count: str | None, expected_status: str, expected_value: int | None
) -> None:
    """Expose the remaining copies separately from the final Move and retain the reported value in XML."""

    count_element = '' if count is None else f'<av:COGCount>{count}</av:COGCount>'
    objects = parse_didl(
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:av="urn:schemas-sony-com:av"><item id="test-recording" parentID="0">'
        f'{count_element}</item></DIDL-Lite>',
        NASNE,
    )
    recording = objects[0]
    assert isinstance(recording, Recording)
    remaining = recording.protection.remaining_copy_count
    assert remaining.status == expected_status
    assert remaining.value == expected_value
    assert remaining.source == 'metadata'
    if count is not None:
        assert f'>{count}</' in recording.raw_xml
