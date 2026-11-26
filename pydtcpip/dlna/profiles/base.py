"""Profiles describing device-family behavior."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from xml.etree import ElementTree as ET

from pydtcpip.dlna.description import DeviceDescription
from pydtcpip.errors import PyDTCPIPError
from pydtcpip.models import (
    ChapterResource,
    ProtectionValue,
    Recording,
    ReportedCopyCount,
    Resource,
    ResourceConversion,
    ResourceVariant,
)


@dataclass(frozen=True)
class CopyCountField:
    """Location and counting convention of a family-specific copy-count field."""

    name: str
    on_resource: bool = False
    includes_final_move: bool | None = None
    unlimited_values: tuple[str, ...] = ()
    is_allowed_use: bool = False


@dataclass(frozen=True)
class ChapterField:
    """Location and document format of a chapter reference."""

    name: str
    format: Literal['sony', 'panasonic', 'sharp']
    on_resource: bool = False


@dataclass(frozen=True)
class Profile:
    """
    Device-family behavior, organized in a tree rooted at generic DLNA.
    Child profiles define only differences from their parents; None values are resolved through the parent chain.
    The root defines standard DLNA behavior. Profiles are stateless and contain only settings and functions
    that take raw data and return values.
    """

    # Family name (matching the filename)
    name: str
    # Parent profile (None only for the generic root)
    parent: 'Profile | None'
    # Match a device description (only devices matching the parent are eligible)
    matches: Callable[[DeviceDescription], bool] | None = None
    # Read remaining copies from a recording's DIDL-Lite item
    read_remaining_copy_count: Callable[[ET.Element], ProtectionValue[int]] | None = None
    # Interpret an HTTP status as a family-specific exception (or return None)
    interpret_http_status: Callable[[int, str, int | None], PyDTCPIPError | None] | None = None
    # Whether BrowseDirectChildren includes fields absent from BrowseMetadata
    has_listing_only_metadata: bool | None = None
    # New families remain unverified until their behavior has been tested with a device
    is_unverified: bool = False
    copy_count_fields: tuple[CopyCountField, ...] | None = None
    chapter_fields: tuple[ChapterField, ...] | None = None
    # Communication is implemented by the extension registry, never by a profile
    extension: str | None = None
    recording_roots: tuple[str, ...] | None = None
    # Interpret resource conversion only when protocolInfo omits DLNA.ORG_CI
    read_resource_conversion: Callable[[ET.Element, ET.Element], ResourceConversion | None] | None = None
    # Classify separately stored recording versions and dedicated transfer URLs
    read_resource_variant: Callable[[Resource], ResourceVariant | None] | None = None
    # Decide whether an individual recording still needs its parent listing
    needs_listing_metadata: Callable[[Recording], bool] | None = None
    # Recognized resource element names, including vendor-advertised alternatives
    resource_elements: tuple[str, ...] | None = None
    # Expose recorder-provided display details without changing the original attributes
    read_resource_details: Callable[[Resource], Resource] | None = None
    # Whether the family's SOAP server accepts requests on a reused TCP connection
    reuse_soap_connections: bool | None = None
    # Whether the source accepts Move control requests on a new connection without a second authentication exchange
    move_reconnect_without_authentication: Callable[[DeviceDescription], bool] | None = None
    # Seconds to wait after receiving a complete Move before requesting finalization
    move_finalize_delay: Callable[[DeviceDescription], float] | None = None

    def resolve_move_finalize_delay(self, description: DeviceDescription) -> float:
        """
        Resolve the wait before Move finalization for the source.

        Args:
            description (DeviceDescription): Source device description

        Returns:
            float: Seconds to wait after receipt before sending the finalize request (defaults to 0)
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.move_finalize_delay is not None:
                return profile.move_finalize_delay(description)
            profile = profile.parent
        return 0.0

    def uses_move_reconnect_without_authentication(self, description: DeviceDescription) -> bool:
        """
        Resolve the Move reconnection policy for the source.

        Args:
            description (DeviceDescription): Source device description

        Returns:
            bool: Whether to omit authentication when reconnecting for this Move
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.move_reconnect_without_authentication is not None:
                return profile.move_reconnect_without_authentication(description)
            profile = profile.parent
        return False

    def allows_soap_connection_reuse(self) -> bool:
        """
        Resolve the SOAP connection policy through the profile tree.

        Returns:
            bool: Whether to reuse connections for SOAP requests (defaults to True)
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.reuse_soap_connections is not None:
                return profile.reuse_soap_connections
            profile = profile.parent
        return True

    def resolve_resource_elements(self) -> tuple[str, ...]:
        """
        Resolve resource element names through the profile tree.

        Returns:
            tuple[str, ...]: Qualified element names accepted by the nearest profile
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.resource_elements is not None:
                return profile.resource_elements
            profile = profile.parent
        return ('{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}res',)

    def resource_details(self, resource: Resource) -> Resource:
        """
        Interpret device-specific recording mode and frame-rate metadata.

        Args:
            resource (Resource): Resource with its standard attributes already interpreted

        Returns:
            Resource: Resource enriched by the nearest handler, or unchanged without one
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.read_resource_details is not None:
                return profile.read_resource_details(resource)
            profile = profile.parent
        return resource

    def resource_variant(self, resource: Resource) -> ResourceVariant | None:
        """
        Identify a recording version using family-specific resource metadata.

        Args:
            resource (Resource): Resource with its original protocol and conversion information

        Returns:
            ResourceVariant | None: Recording version from the nearest handler, or None when unclassified
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.read_resource_variant is not None:
                return profile.read_resource_variant(resource)
            profile = profile.parent
        return None

    def resource_conversion(self, item: ET.Element, resource: ET.Element) -> ResourceConversion | None:
        """
        Interpret family-specific evidence of resource conversion.

        Args:
            item (ET.Element): Complete DIDL-Lite item containing resource references
            resource (ET.Element): Resource whose standard conversion flag is absent

        Returns:
            ResourceConversion | None: Conversion status from the nearest profile handler, or None when unknown
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.read_resource_conversion is not None:
                return profile.read_resource_conversion(item, resource)
            profile = profile.parent
        return None

    def resolve_copy_count_fields(self) -> tuple[CopyCountField, ...]:
        """
        Resolve copy-count fields through the profile tree.

        Returns:
            tuple[CopyCountField, ...]: Fields from the nearest family that defines them
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.copy_count_fields is not None:
                return profile.copy_count_fields
            profile = profile.parent
        return ()

    def read_copy_counts(self, item: ET.Element) -> tuple[ReportedCopyCount, ...]:
        """
        Read all advertised counts, preserving resource identity and special values.

        Args:
            item (ET.Element): Recording's DIDL-Lite item

        Returns:
            tuple[ReportedCopyCount, ...]: Observed fields in profile priority order
        """

        counts: list[ReportedCopyCount] = []
        observed_at = datetime.now(UTC)
        for field in self.resolve_copy_count_fields():
            elements = (
                [child for child in item if child.tag in self.resolve_resource_elements()]
                if field.on_resource
                else [item]
            )
            for element in elements:
                raw_value = element.get(field.name) if field.on_resource else element.findtext(field.name)
                if raw_value is None:
                    continue
                value_text = raw_value.strip()
                if field.is_allowed_use is True:
                    parts = value_text.split(':')
                    value_text = parts[2] if len(parts) == 3 else ''
                is_unlimited = value_text.casefold() in field.unlimited_values
                value: int | None = None
                if is_unlimited is False:
                    try:
                        value = int(value_text, 16 if value_text.lower().startswith('0x') else 10)
                    except ValueError:
                        # Preserve malformed fields as unsupported observations
                        value = None
                    if value is not None and value < 0:
                        value = None
                counts.append(
                    ReportedCopyCount(
                        field_name=field.name,
                        raw_value=raw_value,
                        status='known' if value is not None or is_unlimited is True else 'unsupported',
                        value=value,
                        is_unlimited=is_unlimited,
                        includes_final_move=field.includes_final_move,
                        resource_url=(element.text or '').strip() if field.on_resource else None,
                        observed_at=observed_at,
                    )
                )
        return tuple(counts)

    def read_chapter_resources(self, item: ET.Element) -> tuple[ChapterResource, ...]:
        """
        Read chapter references without fetching the chapter documents.

        Args:
            item (ET.Element): Recording's DIDL-Lite item

        Returns:
            tuple[ChapterResource, ...]: Distinct chapter references in preference order
        """

        profile: Profile | None = self
        while profile is not None and profile.chapter_fields is None:
            profile = profile.parent
        if profile is None or profile.chapter_fields is None:
            return ()
        references: list[ChapterResource] = []
        for field in profile.chapter_fields:
            elements = (
                [child for child in item if child.tag in self.resolve_resource_elements()]
                if field.on_resource
                else [item]
            )
            for element in elements:
                value = element.get(field.name) if field.on_resource else element.findtext(field.name)
                if value is not None and value.strip():
                    reference = ChapterResource(url=value.strip(), format=field.format, source_field=field.name)
                    if reference not in references:
                        references.append(reference)
        return tuple(references)

    def resolve_extension(self) -> str | None:
        """
        Resolve the communication extension through the profile tree.

        Returns:
            str | None: Name of the nearest family's extension, if provided
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.extension is not None:
                return profile.extension
            profile = profile.parent
        return None

    def resolve_recording_roots(self) -> tuple[str, ...]:
        """
        Resolve the family's recording containers.

        Returns:
            tuple[str, ...]: Container IDs, defaulting to the root for recursive browsing
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.recording_roots is not None:
                return profile.recording_roots
            profile = profile.parent
        return ('0',)

    @property
    def depth(self) -> int:
        """Return the depth from the generic root (zero for the root)."""

        depth = 0
        profile = self.parent
        while profile is not None:
            depth += 1
            profile = profile.parent
        return depth

    def is_match(self, description: DeviceDescription) -> bool:
        """
        Return whether the device matches this profile and all its ancestors.

        Args:
            description (DeviceDescription): Recorder device description

        Returns:
            bool: True if the device matches this family
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.matches is not None and profile.matches(description) is False:
                return False
            profile = profile.parent
        return True

    def resolve_remaining_copy_count(self) -> Callable[[ET.Element], ProtectionValue[int]]:
        """
        Resolve the copy count reader through the parent chain.

        Returns:
            Callable[[ET.Element], ProtectionValue[int]]: Function defined by the nearest ancestor
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.read_remaining_copy_count is not None:
                return profile.read_remaining_copy_count
            profile = profile.parent
        raise LookupError('The generic root profile has no read_remaining_copy_count function.')

    def resolve_http_status(self) -> Callable[[int, str, int | None], PyDTCPIPError | None]:
        """
        Resolve the HTTP status interpreter through the parent chain.

        Returns:
            Callable[[int, str, int | None], PyDTCPIPError | None]: Function defined by the nearest ancestor
        """

        profile: Profile | None = self
        while profile is not None:
            if profile.interpret_http_status is not None:
                return profile.interpret_http_status
            profile = profile.parent
        raise LookupError('The generic root profile has no interpret_http_status function.')

    def resolve_has_listing_only_metadata(self, recording: Recording | None = None) -> bool:
        """
        Resolve whether listings contain fields absent from individual recording metadata.

        Args:
            recording (Recording | None): Individual metadata to check, if already available

        Returns:
            bool: Value defined by the nearest ancestor
        """

        profile: Profile | None = self
        while profile is not None:
            if recording is not None and profile.needs_listing_metadata is not None:
                return profile.needs_listing_metadata(recording)
            if profile.has_listing_only_metadata is not None:
                return profile.has_listing_only_metadata
            profile = profile.parent
        raise LookupError('The generic root profile has no has_listing_only_metadata value.')
