"""Panasonic DIGA and VIERA metadata."""

from urllib.parse import parse_qs, urljoin, urlsplit
from xml.etree import ElementTree as ET

from pydtcpip.dlna.description import DeviceDescription
from pydtcpip.dlna.profiles.base import ChapterField, CopyCountField, Profile
from pydtcpip.dlna.profiles.generic import GENERIC
from pydtcpip.errors import ClientNotAllowedError, PyDTCPIPError
from pydtcpip.models import Recording, Resource, ResourceConversion


# UPnP error returned when the recorder refuses to list a protected container's children
## Returned on BrowseDirectChildren of recording folders while BrowseMetadata on the same folder succeeds
_CANNOT_PROCESS_REQUEST = 720


def _matches(description: DeviceDescription) -> bool:
    """
    Match Panasonic media servers using their manufacturer declaration.

    Args:
        description (DeviceDescription): Device description to inspect

    Returns:
        bool: Whether the manufacturer is Panasonic
    """

    model = description.model_name.upper()
    is_stb = 'CATV DIGITAL STB' in model or model in ('C01AS3', 'STW2000', 'IS1050')
    return 'PANASONIC' in description.manufacturer.upper() and is_stb is False


def _read_resource_conversion(item: ET.Element, resource: ET.Element) -> ResourceConversion | None:
    """
    Identify converted resources whose protocolInfo omits DLNA.ORG_CI.

    Args:
        item (ET.Element): Recording with Panasonic HD and VGA references
        resource (ET.Element): Resource to classify

    Returns:
        ResourceConversion | None: Conversion status, or None when the format remains unknown
    """

    url = (resource.text or '').strip()
    if resource.tag == '{urn:schemas-panasonic-com:pxn}EncContentURI' or '1' in parse_qs(urlsplit(url).query).get(
        'CI', []
    ):
        return ResourceConversion(is_converted=True)
    # HD/VGA references can identify derivative resources even when their URLs have no conversion query
    for sibling in item.findall('{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}res'):
        for name in ('HDContentUri', 'VgaContentUri'):
            field_name = f'{{urn:schemas-panasonic-com:pxn}}{name}'
            reference = sibling.get(field_name)
            if reference is not None and reference.strip() and urljoin(url, reference.strip()) == url:
                return ResourceConversion(is_converted=True)
    return None


def _read_resource_details(resource: Resource) -> Resource:
    """
    Retain Panasonic's advertised recording-mode name.

    Args:
        resource (Resource): Resource with the original Panasonic attributes

    Returns:
        Resource: Resource carrying its device-provided mode name
    """

    return resource.model_copy(update={'recording_mode': resource.attributes.get('recMode', resource.recording_mode)})


def _needs_listing_metadata(recording: Recording) -> bool:
    """
    Request listing metadata only when the selected resource has no usable copy count.

    Args:
        recording (Recording): Recording returned by BrowseMetadata

    Returns:
        bool: Whether the parent listing may supply a missing copy count
    """

    resource = recording.original_move_resource
    return not any(
        (count.value is not None or count.is_unlimited is True)
        and (count.resource_url is None or (resource is not None and count.resource_url == resource.url))
        for count in recording.protection.reported_copy_counts
    )


def _interpret_http_status(status: int, url: str, upnp_error_code: int | None) -> PyDTCPIPError | None:
    """
    Interpret a recorder that withholds protected recordings from unauthorized clients.

    Args:
        status (int): HTTP status code
        url (str): URL of the failed request
        upnp_error_code (int | None): UPnP error code from a SOAP fault, if present

    Returns:
        PyDTCPIPError | None: Client authorization error when the recorder refuses the listing, otherwise None
    """

    # Some recorders list structure, photos, music and tuner channels but return 720 for protected
    # recording folders until the PC is authorized, while BrowseMetadata on the same folder still succeeds
    if upnp_error_code == _CANNOT_PROCESS_REQUEST:
        return ClientNotAllowedError(
            'The recorder refused to list its protected recordings. '
            'Enable home streaming and allow this PC in the recorder home-network settings; '
            'some recorders list protected recordings only to a client authenticated with DTCP-IP credentials.',
            http_status=status,
            upnp_error_code=upnp_error_code,
        )
    return None


def _move_finalize_delay(description: DeviceDescription) -> float:
    """
    Allow time for the DMR-BRW1060 to accept finalization after receiving a recording.

    Args:
        description (DeviceDescription): Source device description

    Returns:
        float: Wait before requesting Move finalization, in seconds
    """

    if (description.model_number or description.model_name).upper() == 'DMR-BRW1060':
        return 5.0
    return 0.0


PANASONIC = Profile(
    name='panasonic',
    parent=GENERIC,
    matches=_matches,
    is_unverified=True,
    interpret_http_status=_interpret_http_status,
    # Some models omit CopyCount from BrowseMetadata but include it in parent listings
    has_listing_only_metadata=True,
    needs_listing_metadata=_needs_listing_metadata,
    # Finite counts include the final Move; CF and -1 denote unrestricted copies
    copy_count_fields=(
        CopyCountField(
            '{urn:schemas-panasonic-com:pxn}CopyCount',
            on_resource=True,
            includes_final_move=True,
            unlimited_values=('cf', '-1'),
        ),
    ),
    chapter_fields=(ChapterField('{urn:schemas-panasonic-com:pxn}ChapterList', 'panasonic', on_resource=True),),
    read_resource_conversion=_read_resource_conversion,
    resource_elements=(
        '{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}res',
        '{urn:schemas-panasonic-com:pxn}EncContentURI',
    ),
    read_resource_details=_read_resource_details,
    move_finalize_delay=_move_finalize_delay,
)
