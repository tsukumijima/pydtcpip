"""Exercise DTCPClient streaming and GET Move against a fake recorder on 127.0.0.1."""

import asyncio
import os
import time
from pathlib import Path

import pytest

from pydtcpip.client import DTCPClient
from pydtcpip.dlna.protocol_info import parse_protocol_info
from pydtcpip.dtcp.ake import open_ake_connection as _open_ake_connection
from pydtcpip.errors import DLNAError, DTCPError, MoveCommitmentRejectedError
from pydtcpip.events import MediaInfoReceived, TransferEvent
from pydtcpip.models import MoveResumeState, Resource, TimeRange
from pydtcpip.operations import resume_state_path, save_resume_state
from pydtcpip.sinks import FileSink
from tests.fakes import (
    FakeAuthority,
    FakeDevice,
    FakeMediaServer,
    FakeSourceAKE,
    Framing,
    MemorySink,
    Truncation,
    encrypted_length,
)


# Recording split into four packets, with the last plaintext length not divisible by the AES block size
_CONTENT = os.urandom(4000 * 3 + 1234)


def _resource_for_server(url: str, dtcp_port: int, dtcp_host: str = '127.0.0.1') -> Resource:
    """Build a DTCP-protected Resource with its authentication endpoint in protocolInfo."""
    return Resource(
        url=url,
        protocol_info=parse_protocol_info(
            f'http-get:*:application/x-dtcp1;DTCP1HOST={dtcp_host};DTCP1PORT={dtcp_port};'
            'CONTENTFORMAT="video/mpeg":DLNA.ORG_CI=0'
        ),
        attributes={},
        raw_xml='',
    )


@pytest.mark.anyio
@pytest.mark.parametrize('framing', ['length', 'chunked'])
async def test_stream_decrypts_all_data(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, framing: Framing
) -> None:
    """Decrypt all data when HTTP chunks cross packet boundaries and the final packet contains padding."""

    received = bytearray()
    events: list[TransferEvent] = []

    async def on_data(chunk: bytes) -> None:
        received.extend(chunk)

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT, framing=framing) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        size = await client.stream(server.url, on_data, on_event=events.append)

    assert bytes(received) == _CONTENT
    assert size == len(_CONTENT)
    assert source.errors == []
    responses = [event for event in events if isinstance(event, MediaInfoReceived)]
    assert [event.method for event in responses] == ['HEAD', 'GET']
    assert responses[1].media_info.headers['Content-Type'].startswith('application/x-dtcp1;')


@pytest.mark.anyio
async def test_stream_uses_resource_protocol_info_when_http_content_type_omits_dtcp_endpoint(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Use the advertised Resource endpoint when HEAD and GET omit DTCP parameters."""

    received = bytearray()

    async def on_data(chunk: bytes) -> None:
        received.extend(chunk)

    content_type = 'video/vnd.dlna.mpeg-tts'

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(
            source,
            _CONTENT,
            content_types={'HEAD': content_type, 'GET': content_type},
        ) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        resource = _resource_for_server(server.url, source.port)
        size = await client.stream(resource, on_data)

    assert bytes(received) == _CONTENT
    assert size == len(_CONTENT)
    assert source.errors == []


@pytest.mark.anyio
async def test_stream_accepts_case_insensitive_head_and_get_dtcp_hosts(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept matching DTCP hosts that differ only in letter casing."""

    async def open_local_ake_connection(_host: str, port: int, *, timeout: float):
        return await _open_ake_connection('127.0.0.1', port, timeout=timeout)

    monkeypatch.setattr(
        'pydtcpip.client.open_ake_connection',
        open_local_ake_connection,
    )

    received = bytearray()

    async def on_data(chunk: bytes) -> None:
        received.extend(chunk)

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(
            source,
            _CONTENT,
            content_types={
                'HEAD': 'video/vnd.dlna.mpeg-tts',
                'GET': (f'application/x-dtcp1;DTCP1HOST=localhost;DTCP1PORT={source.port};CONTENTFORMAT="video/mpeg"'),
            },
        ) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        resource_url = server.url.replace('127.0.0.1', 'localhost')
        resource = _resource_for_server(resource_url, source.port, dtcp_host='LOCALHOST')
        size = await client.stream(resource, on_data)

    assert bytes(received) == _CONTENT
    assert size == len(_CONTENT)
    assert source.errors == []


@pytest.mark.anyio
async def test_stream_rejects_different_head_and_get_dtcp_endpoints(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Reject a GET authentication endpoint that differs from the HEAD endpoint."""

    async def on_data(chunk: bytes) -> None:
        pytest.fail('A mismatched DTCP endpoint must not produce media bytes')

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(
            source,
            _CONTENT,
            content_types={
                'HEAD': 'video/vnd.dlna.mpeg-tts',
                'GET': ('application/x-dtcp1;DTCP1HOST=127.0.0.1;DTCP1PORT=1;CONTENTFORMAT="video/mpeg"'),
            },
        ) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        resource = _resource_for_server(server.url, source.port)
        with pytest.raises(DTCPError, match='different DTCP-IP authentication endpoints'):
            await client.stream(resource, on_data)


@pytest.mark.anyio
@pytest.mark.parametrize('method', ['HEAD', 'GET'])
async def test_stream_rejects_redirects_when_using_resource_endpoint(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, method: str
) -> None:
    """Reject a redirect before treating its response as media."""

    async def on_data(chunk: bytes) -> None:
        pytest.fail('A redirect must not produce media bytes')

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(
            source, _CONTENT, content_types={'HEAD': 'video/vnd.dlna.mpeg-tts', 'GET': 'video/vnd.dlna.mpeg-tts'}
        ) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        server.http_errors[method] = (302, {'Location': server.url + '?redirect', 'Content-Type': 'text/html'})
        resource = _resource_for_server(server.url, source.port)
        with pytest.raises(DLNAError) as error:
            await client.stream(resource, on_data)
        assert error.value.http_status == 302
        assert server.count('GET') == (1 if method == 'GET' else 0)
        assert source.log.count('challenge') == (1 if method == 'GET' else 0)


@pytest.mark.anyio
@pytest.mark.parametrize('method', ['HEAD', 'GET'])
async def test_media_http_errors_retain_diagnostics_without_credentials(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, method: str
) -> None:
    """Surface the diagnostic fields in Issue #11 without leaking unrelated HTTP headers."""

    async def on_data(chunk: bytes) -> None:
        pytest.fail('An HTTP error must not produce media bytes')

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        server.http_errors[method] = (
            503,
            {
                'ERROR-INFO': 'LSI0 rsc 541 Sys Busy',
                'error.pxn.panasonic.com': '9009',
                'Set-Cookie': 'private-cookie',
            },
        )
        with pytest.raises(DLNAError) as caught:
            await client.stream(server.url, on_data)
    assert caught.value.http_status == 503
    assert caught.value.diagnostic_headers == {'error-info': 'LSI0 rsc 541 Sys Busy', 'Error.pxn.panasonic.com': '9009'}
    assert '9009' in str(caught.value) and 'Sys Busy' in str(caught.value)
    assert 'private-cookie' not in str(caught.value)


@pytest.mark.anyio
async def test_stream_sends_time_range(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Send the requested time range in TimeSeekRange.dlna.org so the recorder starts at the requested position."""

    async def on_data(chunk: bytes) -> None:
        pass

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        await client.stream(server.url, on_data, time_range=TimeRange(start_ms=60000, end_ms=90500))

    sent_ranges: list[tuple[str, str | None]] = []
    for method, headers in server.requests:
        sent_ranges.append((method, headers.get('TimeSeekRange.dlna.org')))
    assert sent_ranges == [('HEAD', 'npt=60.000-90.500'), ('GET', 'npt=60.000-90.500')]


@pytest.mark.anyio
@pytest.mark.parametrize('framing', ['length', 'chunked'])
async def test_stream_fails_when_body_is_truncated(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, framing: Framing
) -> None:
    """Reject a body truncated within a protected packet instead of reporting a complete recording."""

    async def on_data(chunk: bytes) -> None:
        pass

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT, framing=framing, truncation='middle') as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        with pytest.raises(DTCPError):
            await client.stream(server.url, on_data)


@pytest.mark.anyio
async def test_stream_dispatches_events_in_order(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Events must arrive in order with cumulative byte counts for correct progress reporting."""

    events: list[TransferEvent] = []

    async def on_data(chunk: bytes) -> None:
        pass

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        await client.stream(server.url, on_data, on_event=events.append)

    assert [event.type for event in events] == [
        'media_info_received',
        'authentication_started',
        'peer_certificate_verified',
        'proximity_check_responded',
        'authentication_completed',
        'media_info_received',
        'transfer_started',
        'transfer_protection_observed',
        'transfer_progressed',
        'transfer_progressed',
        'transfer_progressed',
        'transfer_progressed',
        'transfer_received',
    ]

    # Events share a transfer ID and received bytes increase with each protected packet
    assert len({event.transfer_id for event in events}) == 1
    progress: list[int] = []
    for event in events:
        if event.type == 'transfer_started':
            assert event.total_http_bytes == encrypted_length(len(_CONTENT), 4000)
        if event.type == 'transfer_protection_observed':
            assert event.encryption_mode.value == 0x4
        if event.type == 'transfer_progressed':
            progress.append(event.content_bytes)
        if event.type == 'transfer_received':
            assert event.content_bytes == len(_CONTENT)
    assert progress == [4000, 8000, 12000, len(_CONTENT)]


@pytest.mark.anyio
async def test_move_waits_before_finalizing_when_requested(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Delay only the finalize request so sources that need time after the transfer ends can accept it."""

    path = tmp_path / 'recording.ts'
    timestamps: dict[str, float] = {}

    def on_event(event: TransferEvent) -> None:
        timestamps.setdefault(event.type, time.monotonic())

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        size = await client.move(server.url, FileSink(path), on_event=on_event, finalize_delay=0.3)

    assert size == len(_CONTENT)
    assert path.read_bytes() == _CONTENT
    assert source.log == ['mv_initiate', 'challenge', 'rtt', 'mv_exchange_key', 'finalize', 'complete']
    # The recording is persisted before the wait, and finalization is requested only after it
    assert timestamps['move_finalize_requested'] - timestamps['sink_persisted'] >= 0.3
    assert source.errors == []


@pytest.mark.anyio
async def test_move_cancelled_during_finalize_delay_leaves_no_resume_file(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Cancel after persisting the recording and leave the output path available for another Move."""

    path = tmp_path / 'recording.ts'
    sink = FileSink(path)
    resume_path = resume_state_path(path)
    persisted = asyncio.Event()

    def on_event(event: TransferEvent) -> None:
        """
        Notify the test once the recording has been persisted.

        Args:
            event (TransferEvent): Event emitted during the Move
        """

        if event.type == 'sink_persisted':
            persisted.set()

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        move = asyncio.create_task(
            client.move(
                server.url,
                sink,
                on_resume_state=lambda state: save_resume_state(resume_path, state),
                on_event=on_event,
                finalize_delay=5.0,
            )
        )
        # Cancel during the wait, after the complete recording has reached disk
        await asyncio.wait_for(persisted.wait(), timeout=5)
        move.cancel()
        with pytest.raises(asyncio.CancelledError):
            await move

    assert source.log == ['mv_initiate', 'challenge', 'rtt', 'mv_exchange_key', 'cancel']
    assert source.errors == []
    assert sink.pending_path.exists() is False
    assert resume_path.exists() is False


@pytest.mark.anyio
async def test_move_completes(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Finalize and complete the Move in order so the saved recording matches the source copy count."""

    path = tmp_path / 'recording.ts'
    resume_states: list[MoveResumeState | None] = []
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        size = await client.move(server.url, FileSink(path), on_resume_state=resume_states.append)

    assert size == len(_CONTENT)
    assert path.read_bytes() == _CONTENT
    assert (tmp_path / '.recording.ts.part').exists() is False

    # Authenticate, GET the complete recording with the issued key label, then finalize and complete
    assert source.log == ['mv_initiate', 'challenge', 'rtt', 'mv_exchange_key', 'finalize', 'complete']
    assert server.requests[-1][0] == 'GET'
    assert server.requests[-1][1]['BLKMove.dtcp.com'] == f'{source.move_exchange_key_label:02X}'
    assert source.errors == []

    # Resume state is delivered before finalization and after its response, then cleared on completion
    assert len(resume_states) == 3
    assert resume_states[0] is not None and resume_states[0].state == 'finalizing'
    assert resume_states[1] is not None and resume_states[1].state == 'finalized'
    assert resume_states[2] is None


@pytest.mark.anyio
@pytest.mark.parametrize('is_cancel_response_lost', [False, True])
async def test_move_cancels_when_body_ends_at_packet_boundary(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    tmp_path: Path,
    is_cancel_response_lost: bool,
) -> None:
    """
    Do not finalize a recording truncated at a packet boundary.
    Even if the cancellation response is lost, discard partial data and report the original failure.
    """

    path = tmp_path / 'recording.ts'
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT, truncation='boundary') as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        source.drop_cancel_response = is_cancel_response_lost
        with pytest.raises(DTCPError, match='before all data arrived'):
            await client.move(server.url, FileSink(path))

    assert source.log == ['mv_initiate', 'challenge', 'rtt', 'mv_exchange_key', 'cancel']
    assert source.finalize_nonces == []
    assert path.exists() is False
    assert (tmp_path / '.recording.ts.part').exists() is False


@pytest.mark.anyio
async def test_move_refuses_body_without_detectable_end(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    tmp_path: Path,
) -> None:
    """
    Without Content-Length or chunked framing, a disconnect at a packet boundary may look complete.
    Refuse finalization when the decrypted size cannot be checked.
    """

    path = tmp_path / 'recording.ts'
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT, framing='close') as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        with pytest.raises(DTCPError, match='did not report the body length'):
            await client.move(server.url, FileSink(path))

    assert source.log[-1] == 'cancel'
    assert source.finalize_nonces == []
    assert path.exists() is False
    assert (tmp_path / '.recording.ts.part').exists() is False


@pytest.mark.anyio
@pytest.mark.parametrize('truncation', ['none', 'boundary'])
async def test_move_checks_cleartext_size_when_body_end_is_undetectable(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    tmp_path: Path,
    truncation: Truncation,
) -> None:
    """Finalize close-delimited bodies only when their decrypted size matches metadata."""

    path = tmp_path / 'recording.ts'
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT, framing='close', truncation=truncation) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        resource = Resource(
            url=server.url,
            protocol_info=parse_protocol_info('http-get:*:application/x-dtcp1:DLNA.ORG_CI=0'),
            attributes={'{urn:schemas-dlna-org:metadata-1-0/}cleartextSize': str(len(_CONTENT))},
            raw_xml='',
        )
        if truncation == 'none':
            assert await client.move(resource, FileSink(path)) == len(_CONTENT)
        else:
            with pytest.raises(DTCPError, match='before all data arrived'):
                await client.move(resource, FileSink(path))

    if truncation == 'none':
        assert path.read_bytes() == _CONTENT
        assert source.log[-2:] == ['finalize', 'complete']
    else:
        assert path.exists() is False
        assert source.log[-1] == 'cancel'
        assert source.finalize_nonces == []


@pytest.mark.anyio
async def test_rejected_resume_keeps_received_data(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, tmp_path: Path
) -> None:
    """
    DBR-Z620 discards resume state after accepting finalization and rejects a retry.
    Retain received data because the source may have finalized, but clear state that cannot be resumed.
    """

    path = tmp_path / 'recording.ts'
    resume_states: list[MoveResumeState | None] = []
    events: list[TransferEvent] = []
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        # Lose every finalize response so the in-band retries are exhausted and the Move is left resumable
        source.drop_finalize_responses = 3
        with pytest.raises(DTCPError):
            await client.move(server.url, FileSink(path), on_resume_state=resume_states.append)
        resume_state = resume_states[-1]
        assert resume_state is not None and resume_state.state == 'finalizing'

        # Do not ask a user-provided sink to discard received data either
        source.reject_finalize = True
        sink = MemorySink()
        with pytest.raises(MoveCommitmentRejectedError) as raised:
            await client.resume_move(resume_state, sink, on_resume_state=resume_states.append, on_event=events.append)

    assert raised.value.may_be_committed is True
    assert sink.calls == []
    assert source.log[-1] == 'finalize_rejected'
    assert resume_states[-1] is None
    assert path.exists() is False
    assert (tmp_path / '.recording.ts.part').read_bytes() == _CONTENT
    assert events[-1].type == 'move_finalize_rejected'


@pytest.mark.anyio
async def test_first_rejected_finalize_discards_received_data(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Discard received data if the first finalize request is rejected and the source recording remains."""

    path = tmp_path / 'recording.ts'
    resume_states: list[MoveResumeState | None] = []
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        source.reject_finalize = True
        with pytest.raises(MoveCommitmentRejectedError) as raised:
            await client.move(server.url, FileSink(path), on_resume_state=resume_states.append)

    assert raised.value.may_be_committed is False
    assert resume_states[-1] is None
    assert path.exists() is False
    assert (tmp_path / '.recording.ts.part').exists() is False


@pytest.mark.anyio
async def test_finalize_rejected_after_response_is_lost_may_be_committed(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, tmp_path: Path
) -> None:
    """
    A finalize retry rejected after the first response was lost may already have been accepted.
    Retain received data because the source consumed a copy when it accepted the first request.
    """

    path = tmp_path / 'recording.ts'
    resume_states: list[MoveResumeState | None] = []
    events: list[TransferEvent] = []
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        # Accept the first finalize request but drop its response, then reject the in-band retry
        source.drop_finalize_responses = 1
        source.reject_finalize_after_accepts = 1
        with pytest.raises(MoveCommitmentRejectedError) as raised:
            await client.move(server.url, FileSink(path), on_resume_state=resume_states.append, on_event=events.append)

    assert raised.value.may_be_committed is True
    assert source.log[-1] == 'finalize_rejected'
    assert len(source.finalize_nonces) == 1
    assert resume_states[-1] is None
    assert path.exists() is False
    assert (tmp_path / '.recording.ts.part').read_bytes() == _CONTENT
    assert events[-1].type == 'move_finalize_rejected'


@pytest.mark.anyio
async def test_move_retries_finalize_on_a_new_connection_after_mismatched_response(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Discard a connection whose response names a different command and retry finalization on a new one."""

    path = tmp_path / 'recording.ts'
    resume_states: list[MoveResumeState | None] = []
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        source.wrong_command_finalize_responses = 1
        size = await client.move(server.url, FileSink(path), on_resume_state=resume_states.append)

    assert size == len(_CONTENT)
    assert path.read_bytes() == _CONTENT
    assert 'cancel' not in source.log
    assert len(source.finalize_nonces) == 2
    assert source.finalize_nonces[0] == source.finalize_nonces[1]
    assert source.log[-2:] == ['finalize', 'complete']
    # The mismatched response discards the first connection, so the retry authenticates on a second one
    assert source.connection_count == 2
    assert resume_states[-1] is None


@pytest.mark.anyio
async def test_rejected_move_initiation_explains_the_refusal(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Report a refusal to start the Move before receiving any recording data."""

    path = tmp_path / 'recording.ts'
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        source.reject_initiate = True
        with pytest.raises(DTCPError, match=r'refused to start the Move.*status: 0x1'):
            await client.move(server.url, FileSink(path))

    assert source.log == ['mv_initiate_rejected']
    assert path.exists() is False
    assert (tmp_path / '.recording.ts.part').exists() is False


@pytest.mark.anyio
async def test_move_completes_when_source_rejects_complete(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Keep the recording if completion is rejected after a verified finalize response."""

    path = tmp_path / 'recording.ts'
    resume_states: list[MoveResumeState | None] = []
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        source.reject_complete = True
        assert await client.move(server.url, FileSink(path), on_resume_state=resume_states.append) == len(_CONTENT)

    assert path.read_bytes() == _CONTENT
    assert source.log[-2:] == ['finalize', 'complete_rejected']
    assert resume_states[-1] is None


@pytest.mark.anyio
async def test_recording_is_committed_before_complete_response(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Commit after verified finalization and retry only completion if its responses are lost."""

    path = tmp_path / 'recording.ts'
    resume_states: list[MoveResumeState | None] = []
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        # Lose every complete response so the in-band retries are exhausted
        source.drop_complete_responses = 3
        with pytest.raises(DTCPError):
            await client.move(server.url, FileSink(path), on_resume_state=resume_states.append)
        assert path.read_bytes() == _CONTENT
        resume_state = resume_states[-1]
        assert resume_state is not None and resume_state.state == 'finalized'

        # The sink is committed, so retry only completion without a sink
        assert await client.resume_move(resume_state, None, on_resume_state=resume_states.append) == len(_CONTENT)

    assert source.log[-1] == 'complete'
    assert source.log.count('complete') == 4
    assert resume_states[-1] is None


@pytest.mark.anyio
@pytest.mark.parametrize('drop_reconnect_authentication', [0, 1])
async def test_move_retries_finalize_after_response_is_lost(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    tmp_path: Path,
    drop_reconnect_authentication: int,
) -> None:
    """Retry finalization on a fresh connection with the same nonce when its response is lost."""

    path = tmp_path / 'recording.ts'
    pending_path = tmp_path / '.recording.ts.part'
    resume_states: list[MoveResumeState | None] = []
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        source.drop_finalize_responses = 1
        source.require_authenticated_finalize = True
        source.drop_reconnect_authentication = drop_reconnect_authentication
        size = await client.move(server.url, FileSink(path), on_resume_state=resume_states.append)

    assert size == len(_CONTENT)
    assert path.read_bytes() == _CONTENT
    assert pending_path.exists() is False
    assert 'cancel' not in source.log
    assert source.log.count('authentication_disconnected') == drop_reconnect_authentication
    assert 'unauthenticated_finalize_rejected' not in source.log
    assert len(source.finalize_nonces) == 2
    assert source.finalize_nonces[0] == source.finalize_nonces[1]
    assert source.log[-2:] == ['finalize', 'complete']
    assert resume_states[-1] is None


@pytest.mark.anyio
async def test_move_resumes_after_finalize_response_is_lost(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    tmp_path: Path,
) -> None:
    """Retain data and resume state after every finalize response is lost because the source may have consumed a copy."""

    path = tmp_path / 'recording.ts'
    pending_path = tmp_path / '.recording.ts.part'
    resume_states: list[MoveResumeState | None] = []
    events: list[TransferEvent] = []
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        # Lose every finalize response so the in-band retries are exhausted and the Move is left resumable
        source.drop_finalize_responses = 3
        with pytest.raises(DTCPError):
            await client.move(server.url, FileSink(path), on_resume_state=resume_states.append, on_event=events.append)

        # After finalization was requested, retain the complete data and resume state without cancelling
        assert 'cancel' not in source.log
        assert path.exists() is False
        assert pending_path.read_bytes() == _CONTENT
        resume_state = resume_states[-1]
        assert resume_state is not None
        assert resume_state.state == 'finalizing'
        assert events[-1].type == 'move_resume_required'

        # Retry with the same nonce without receiving data or obtaining a new Move key
        ## Authenticate normally first because DBR-Z620 closes unauthenticated finalize requests
        get_count = server.count('GET')
        log_count = len(source.log)
        size = await client.resume_move(resume_state, FileSink.resume(path), on_resume_state=resume_states.append)

    assert size == len(_CONTENT)
    assert path.read_bytes() == _CONTENT
    assert pending_path.exists() is False
    assert server.count('GET') == get_count
    assert 'mv_initiate' not in source.log[log_count:]
    assert 'mv_exchange_key' not in source.log[log_count:]
    assert source.log[log_count] == 'challenge'
    assert len(source.finalize_nonces) == 4
    assert len(set(source.finalize_nonces)) == 1
    assert source.log[-2:] == ['finalize', 'complete']
    assert resume_states[-1] is None
