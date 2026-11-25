"""Operations shared by the CLI and the TUI for resource selection, Move, resumption and copy-count queries."""

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import ValidationError

from pydtcpip.client import DTCPClient
from pydtcpip.dlna.client import MediaServer
from pydtcpip.dlna.description import parse_device_description
from pydtcpip.dlna.profiles import select_profile
from pydtcpip.errors import DLNAError, DTCPError
from pydtcpip.events import EventCallback, MediaInfoReceived, TransferEvent, dispatch_event
from pydtcpip.metadata import MetadataSidecar
from pydtcpip.models import Container, MoveResumeState, ProtectionValue, Recording, RecordingMetadata, Resource
from pydtcpip.sinks import FileSink


_LOGGER = logging.getLogger(__name__)


class ResourceSelectionError(ValueError):
    """The requested resource cannot be used for the operation."""


def write_private_file(path: Path, data: bytes) -> None:
    """
    Replace a file after writing it with owner-only read and write permissions.

    Args:
        path (Path): Destination file
        data (bytes): Contents to persist
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as file:
        os.chmod(file.name, 0o600)
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(file.name, path)


def resume_state_path(output: Path) -> Path:
    """
    Derive the resume-state path for a Move destination.

    Args:
        output (Path): Recording destination

    Returns:
        Path: Adjacent resume-state file
    """

    return output.with_name(f'{output.name}.pydtcpip-resume.json')


def save_resume_state(path: Path, state: MoveResumeState | None) -> None:
    """
    Save Move resume state with owner-only read permission, or delete it when the Move completes.

    Args:
        path (Path): Resume-state file
        state (MoveResumeState | None): State to save, or None to remove the file
    """

    if state is None:
        path.unlink(missing_ok=True)
        return
    write_private_file(path, state.model_dump_json().encode('utf-8'))


def load_resume_state(output: Path) -> MoveResumeState:
    """
    Load the resume state saved beside a Move destination.

    Args:
        output (Path): Move destination

    Returns:
        MoveResumeState: Saved resume state

    Raises:
        OSError: If the resume state cannot be read
        ValidationError: If the resume state is malformed
    """

    return MoveResumeState.model_validate_json(resume_state_path(output).read_text(encoding='utf-8'))


def load_resume_sidecar(output: Path) -> MetadataSidecar | None:
    """
    Load the metadata archive beside an interrupted Move without letting it block recovery.

    Args:
        output (Path): Move destination

    Returns:
        MetadataSidecar | None: Archive to update, or None when it is absent or unreadable
    """

    # Optional metadata must not prevent recovery after the source may have consumed a copy
    try:
        return MetadataSidecar.resume(output)
    except (ValidationError, OSError) as ex:
        _LOGGER.warning(
            f'Could not load recording metadata beside {output}; continuing Move recovery without metadata updates.',
            exc_info=ex,
        )
        return None


def existing_move_outputs(output: Path) -> list[Path]:
    """
    List files that a new Move to this destination would overwrite or confuse with a previous Move.

    Args:
        output (Path): Recording destination

    Returns:
        list[Path]: Existing recording, metadata and resume-state files
    """

    paths = [output, resume_state_path(output), output.with_suffix('.xml'), output.with_suffix('.metadata.json')]
    return [path for path in paths if path.exists()]


def is_broadcast(recording: Recording) -> bool:
    """
    Determine whether an item is a live channel rather than a recording.

    Args:
        recording (Recording): Item to inspect

    Returns:
        bool: True for a live channel
    """

    # DBR-Z620 lists live channels as videoBroadcast items in the LIVE_TUNER folder
    return recording.upnp_class.startswith('object.item.videoItem.videoBroadcast')


def is_protected(recording: Recording) -> bool:
    """
    Determine whether the source protects a recording, which prevents moving it until the protection is released.

    Args:
        recording (Recording): Recording to inspect

    Returns:
        bool: True when upnp:writeStatus is PROTECTED
    """

    return recording.write_status == 'PROTECTED'


def select_resource(recording: Recording, *, for_move: bool = False, resource_index: int | None = None) -> Resource:
    """
    Select the stored recording by default, or an explicitly requested resource.

    Args:
        recording (Recording): Recording whose resources are inspected
        for_move (bool): Select the original-quality Move endpoint instead of the streaming endpoint
        resource_index (int | None): One-based resource number from info, or None for the default selection

    Returns:
        Resource: Selected DTCP-IP resource

    Raises:
        ResourceSelectionError: If the requested or default resource cannot be used for the operation
    """

    if resource_index is not None:
        if resource_index < 1 or resource_index > len(recording.resources):
            raise ResourceSelectionError(f'Resource {resource_index} is not advertised for this recording.')
        resource = recording.resources[resource_index - 1]
        if resource.protocol_info.is_dtcp is False:
            raise ResourceSelectionError('The selected resource does not use DTCP-IP.')
        if for_move is False and resource.variant is not None and resource.variant.is_move_only is True:
            raise ResourceSelectionError('The selected resource is available only for Move.')
        return resource
    resource = recording.original_move_resource if for_move is True else recording.original_resource
    if resource is None:
        raise ResourceSelectionError(
            'This recording has no original-quality DTCP-IP resource for this operation. '
            'Use info to inspect the available qualities, then select one with --resource or its URL.'
        )
    return resource


def sanitize_title(title: str) -> str:
    """
    Convert a recording title into a filename component that stays within common filesystem limits.

    Args:
        title (str): Recording title

    Returns:
        str: Title without path separators or reserved characters
    """

    title = title.translate(str.maketrans(dict.fromkeys('/\\:*?"<>|\r\n\t', '_')))
    # Truncate long titles, including Japanese titles, so the filename stays under 255 bytes
    return title.encode('utf-8')[:200].decode('utf-8', errors='ignore').rstrip(' .') or 'recording'


def recording_output_path(directory: Path, device_udn: str, recording: Recording) -> Path:
    """
    Choose a Move destination from the title and an identifier unique to the device and recording.
    Recorder object IDs can contain path separators or share long prefixes, so they are hashed.

    Args:
        directory (Path): Destination directory
        device_udn (str): UDN of the source device
        recording (Recording): Recording to save

    Returns:
        Path: Destination for the recording
    """

    identity = f'{device_udn}\n{recording.reference_id or recording.id}'
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]
    return directory / f'{sanitize_title(recording.title)}-{digest}.m2ts'


async def collect_move_metadata(
    media_server: MediaServer, recording: Recording, output: Path
) -> tuple[RecordingMetadata, MetadataSidecar]:
    """
    Query optional metadata and save it beside the destination before the source can delete the recording.

    Args:
        media_server (MediaServer): Source recorder
        recording (Recording): Recording to save
        output (Path): Recording destination

    Returns:
        tuple[RecordingMetadata, MetadataSidecar]: Collected metadata and the archive that records transfer events
    """

    metadata = await media_server.collect_metadata(recording)
    return metadata, MetadataSidecar(output, metadata)


def _archive_events(sidecar: MetadataSidecar | None, on_event: EventCallback | None) -> EventCallback:
    """
    Record each transfer event in the metadata archive before passing it to the caller.

    Args:
        sidecar (MetadataSidecar | None): Metadata archive associated with the transfer
        on_event (EventCallback | None): Caller's event callback

    Returns:
        EventCallback: Callback that archives and forwards events
    """

    async def handle(event: TransferEvent) -> None:
        if sidecar is not None:
            sidecar.on_event(event)
        await dispatch_event(on_event, event)

    return handle


async def run_move(
    client: DTCPClient,
    target: Resource | str,
    output: Path,
    *,
    media_server: MediaServer | None = None,
    sidecar: MetadataSidecar | None = None,
    on_event: EventCallback | None = None,
) -> int:
    """
    Receive a complete recording by GET Move into a file and persist resume state before finalization.

    Args:
        client (DTCPClient): Authenticated DTCP-IP client
        target (Resource | str): Resource or its URL
        output (Path): Recording destination
        media_server (MediaServer | None): Source recorder used for reconnection policy and HTTP errors
        sidecar (MetadataSidecar | None): Metadata archive that records transfer events
        on_event (EventCallback | None): Event callback

    Returns:
        int: Number of recording bytes written
    """

    path = resume_state_path(output)
    try:
        return await client.move(
            target,
            FileSink(output),
            on_resume_state=lambda state: save_resume_state(path, state),
            on_event=_archive_events(sidecar, on_event),
            reconnect_without_authentication=(
                media_server.profile.uses_move_reconnect_without_authentication(media_server.description)
                if media_server is not None
                else False
            ),
            finalize_delay=media_server.profile.resolve_move_finalize_delay() if media_server is not None else 0.0,
        )
    except DLNAError as ex:
        if media_server is not None:
            raise media_server.interpret_http_error(ex, target.url if isinstance(target, Resource) else target) from ex
        raise


async def run_resume(
    client: DTCPClient,
    output: Path,
    resume_state: MoveResumeState,
    *,
    sidecar: MetadataSidecar | None = None,
    on_event: EventCallback | None = None,
) -> int:
    """
    Resume finalization of an interrupted Move without receiving the recording again.

    Args:
        client (DTCPClient): DTCP-IP client
        output (Path): Move destination
        resume_state (MoveResumeState): Saved resume state
        sidecar (MetadataSidecar | None): Metadata archive that records transfer events
        on_event (EventCallback | None): Event callback

    Returns:
        int: Number of committed recording bytes
    """

    # Older resume files omit the connection policy; the metadata archive can identify the source
    if (
        'reconnect_without_authentication' not in resume_state.model_fields_set
        and sidecar is not None
        and sidecar.metadata.device_description_xml is not None
    ):
        try:
            description = parse_device_description(
                sidecar.metadata.device_location or '', sidecar.metadata.device_description_xml
            )
        except DLNAError:
            # Optional metadata must not block recovery
            pass
        else:
            if select_profile(description).uses_move_reconnect_without_authentication(description):
                resume_state = resume_state.model_copy(update={'reconnect_without_authentication': True})

    # If finalization was verified and data is already in place, retry only the completion notification
    sink: FileSink | None = None
    if resume_state.state == 'finalizing' or FileSink(output).pending_path.exists() is True:
        sink = FileSink.resume(output)
    path = resume_state_path(output)
    return await client.resume_move(
        resume_state,
        sink,
        on_resume_state=lambda state: save_resume_state(path, state),
        on_event=_archive_events(sidecar, on_event),
    )


async def save_stream(
    client: DTCPClient,
    target: Resource,
    output: Path,
    *,
    sidecar: MetadataSidecar | None,
    on_event: EventCallback,
) -> int:
    """
    Save an entire recording via ordinary streaming without changing the source copy count.

    Args:
        client (DTCPClient): Client holding the source authentication credentials
        target (Resource): Streaming resource in the recording's stored format
        output (Path): Destination for the recording
        sidecar (MetadataSidecar | None): Existing archive from the attempted Move
        on_event (EventCallback): Callback for receiving progress and media information

    Returns:
        int: Number of recording bytes saved
    """

    sink = FileSink(output)
    expected_size = target.cleartext_size
    archive = _archive_events(sidecar, on_event)

    async def handle_event(event: TransferEvent) -> None:
        await dispatch_event(archive, event)
        if isinstance(event, MediaInfoReceived) and event.method == 'GET':
            media = event.media_info
            is_chunked = any(
                name.lower() == 'transfer-encoding' and 'chunked' in value.lower()
                for name, value in media.headers.items()
            )
            # With no HTTP framing or known recording size, an early disconnect looks like a complete recording
            if expected_size is None and media.content_length is None and is_chunked is False:
                raise DTCPError('The source did not report enough information to verify receipt of the full recording.')

    try:
        written = await client.stream(target, sink.write, on_event=handle_event)
        if written == 0 or (expected_size is not None and written != expected_size):
            raise DTCPError('The stream ended without delivering the expected recording data.')
        await sink.persist()
    except BaseException:
        await sink.discard()
        raise

    # Once publishing the file begins, wait for the result even if the transfer is cancelled
    commit = asyncio.create_task(sink.commit())
    try:
        await asyncio.shield(commit)
    except asyncio.CancelledError:
        await commit
    return written


async def query_copy_count(
    media_server: MediaServer, recording: Recording, resource_url: str | None
) -> ProtectionValue[int] | None:
    """
    Query the source once for the current copy count of a recording.

    Args:
        media_server (MediaServer): Source recorder
        recording (Recording): Recording to query
        resource_url (str | None): Resource URL whose count to read, or None for the default Move resource

    Returns:
        ProtectionValue[int] | None: Current count, or None when the ID now refers to a folder

    Raises:
        DLNAError: If the recording cannot be queried, including when the source deleted it
    """

    item = await media_server.get_metadata(recording.id)
    if isinstance(item, Container):
        return None
    selected = next((resource for resource in item.resources if resource.url == resource_url), None)
    if selected is not None:
        return item.copy_count_for(selected)
    if resource_url is None:
        return item.protection.remaining_copy_count
    return ProtectionValue[int](status='omitted', source='metadata')
