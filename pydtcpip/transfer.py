"""Receive, finalize, and cancel transfers shared by GET Move and incoming transfers."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from pydtcpip.dtcp.move import MoveTransaction
from pydtcpip.dtcp.pcp import ProtectedPacket
from pydtcpip.errors import DTCPError, MoveCommitmentRejectedError
from pydtcpip.events import (
    EventCallback,
    MoveFinalizeRejected,
    MoveResumeRequired,
    SinkPersisted,
    TransferProgressed,
    TransferProtectionObserved,
    TransferReceived,
    dispatch_event,
)
from pydtcpip.models import MoveResumeState, ProtectionValue
from pydtcpip.sinks import Sink


_LOGGER = logging.getLogger(__name__)

# Callback for Move resume state (None when completion or cancellation makes it unnecessary)
type ResumeStateCallback = Callable[[MoveResumeState | None], Awaitable[None] | None]


async def dispatch_transfer_protection(
    on_event: EventCallback | None, transfer_id: str, packet: ProtectedPacket
) -> None:
    """
    Report transfer copy control from the first protected packet header.

    Args:
        on_event (EventCallback | None): Event callback
        transfer_id (str): Transfer identifier for the event
        packet (ProtectedPacket): First received protected packet
    """

    event = TransferProtectionObserved(
        transfer_id=transfer_id,
        encryption_mode=ProtectionValue[int](status='known', value=packet.encryption_mode, source='pcp_header'),
        usage_rules=ProtectionValue[bytes](status='unsupported', source='pcp_header'),
    )
    await dispatch_event(on_event, event)


async def receive_move_body(
    transaction: MoveTransaction,
    packets: AsyncIterator[ProtectedPacket],
    sink: Sink,
    *,
    expected_http_bytes: int | None,
    expected_content_bytes: int | None,
    on_event: EventCallback | None,
) -> None:
    """
    Decrypt Move packets into the sink and verify that the entire recording arrived.
    Raise an exception instead of finalizing if complete receipt cannot be verified.

    Args:
        transaction (MoveTransaction): Move being received
        packets (AsyncIterator[ProtectedPacket]): Protected packets read from the HTTP body
        sink (Sink): Destination for decrypted recording data
        expected_http_bytes (int | None): HTTP body length if provided by Content-Length
        expected_content_bytes (int | None): Decrypted length to check when the HTTP body end cannot be detected
        on_event (EventCallback | None): Event callback
    """

    transfer_id = transaction.transfer_id
    http_bytes = 0
    is_first_packet = True
    async for packet in packets:
        if is_first_packet is True:
            await dispatch_transfer_protection(on_event, transfer_id, packet)
            is_first_packet = False
        cleartext = transaction.decrypt(packet)
        http_bytes += len(packet.wire_bytes)
        if len(cleartext) > 0:
            await sink.write(cleartext)
            transaction.written_bytes += len(cleartext)
        await dispatch_event(
            on_event,
            TransferProgressed(
                transfer_id=transfer_id,
                http_bytes=http_bytes,
                content_bytes=transaction.written_bytes,
                written_bytes=transaction.written_bytes,
            ),
        )

    # PCP parsing cannot distinguish a complete body from a disconnect at a packet boundary; check reported lengths
    if expected_http_bytes is not None and http_bytes != expected_http_bytes:
        raise DTCPError(f'The Move body ended before all data arrived ({http_bytes} / {expected_http_bytes} bytes).')
    if expected_content_bytes is not None and transaction.written_bytes != expected_content_bytes:
        raise DTCPError(
            f'The Move body ended before all data arrived (decrypted: {transaction.written_bytes} / {expected_content_bytes} bytes).'
        )
    if transaction.written_bytes == 0:
        raise DTCPError('The source sent no recording data.')
    transaction.has_received_all = True
    await dispatch_event(
        on_event,
        TransferReceived(transfer_id=transfer_id, http_bytes=http_bytes, content_bytes=transaction.written_bytes),
    )


async def finish_move(
    transaction: MoveTransaction,
    sink: Sink | None,
    *,
    on_resume_state: ResumeStateCallback | None,
    on_event: EventCallback | None,
    before_finalize: Callable[[], Awaitable[None]] | None = None,
    finalize_delay: float = 0.0,
) -> None:
    """
    Finish a received Move by persisting the sink, finalizing, committing the sink, and notifying completion.
    Deliver resume state before finalization and after its response, then None when completion is accepted.

    Args:
        transaction (MoveTransaction): Fully received Move or one restored from resume state
        sink (Sink | None): Recording sink (None if already committed and only completion is being retried)
        on_resume_state (ResumeStateCallback | None): Resume state callback
        on_event (EventCallback | None): Event callback
        before_finalize (Callable[[], Awaitable[None]] | None): Action after saving resume state and before finalization
        finalize_delay (float): Seconds to wait after persisting a newly received recording
    """

    async def save_resume_state(resume_state: MoveResumeState | None) -> None:
        if on_resume_state is not None:
            result = on_resume_state(resume_state)
            if result is not None:
                await result

    if transaction.state in ('receiving', 'finalizing') and sink is None:
        raise DTCPError('A Move requiring finalization needs a sink for the received data.')

    # Send the finalize request only after persisting the recording and saving resume state
    if transaction.state == 'receiving' and sink is not None:
        await sink.persist()
        await dispatch_event(
            on_event, SinkPersisted(transfer_id=transaction.transfer_id, written_bytes=transaction.written_bytes)
        )
        # Keep the wait cancellable and save resume state only when ready to start finalization
        if finalize_delay > 0:
            _LOGGER.info(f'Waiting {finalize_delay:g} seconds for the source before requesting Move finalization.')
            await asyncio.sleep(finalize_delay)
    if transaction.state in ('receiving', 'finalizing'):
        await save_resume_state(transaction.resume_state())
        if before_finalize is not None:
            await before_finalize()
        try:
            await transaction.finalize()
        except MoveCommitmentRejectedError:
            # A rejected finalize request cannot be retried successfully, so clear the resume state
            await save_resume_state(None)
            raise
        await save_resume_state(transaction.resume_state())

    # Make the recording available once a verifiable finalize response arrives (V1SE 10.4.3)
    ## If the completion response is lost, keep the recording committed and retry only the notification
    if sink is not None:
        await sink.commit()
    await transaction.complete()
    await save_resume_state(None)


async def abort_move(transaction: MoveTransaction, sink: Sink | None, *, on_event: EventCallback | None) -> None:
    """
    Handle a failed Move according to whether the finalize request was sent.
    Before finalization, cancel and discard partial data; afterward, retain the full data and request resumption.

    Args:
        transaction (MoveTransaction): Failed Move
        sink (Sink | None): Move sink (None if failure occurred before creation)
        on_event (EventCallback | None): Event callback
    """

    if transaction.state == 'receiving':
        try:
            await transaction.cancel()
        except (OSError, TimeoutError, DTCPError) as ex:
            # Without a cancellation response, the source's outcome is unknown, but finalization was not requested
            _LOGGER.warning('No response was received to the Move cancellation request.', exc_info=ex)
        if sink is not None:
            await sink.discard()
    elif transaction.state in ('finalizing', 'finalized'):
        await dispatch_event(on_event, MoveResumeRequired(transfer_id=transaction.transfer_id, state=transaction.state))
    elif transaction.state == 'rejected':
        # If the first finalize request was rejected, the source did not finalize and partial data can be discarded
        ## After an unknown prior result, retain the data because the source may have finalized
        may_be_committed = transaction.is_finalize_result_unknown
        if may_be_committed is False and sink is not None:
            await sink.discard()
        await dispatch_event(
            on_event, MoveFinalizeRejected(transfer_id=transaction.transfer_id, may_be_committed=may_be_committed)
        )
