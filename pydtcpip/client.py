"""DTCP-IP client for streaming and GET Move."""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

import aiohttp

from pydtcpip.dlna.protocol_info import parse_content_type
from pydtcpip.dtcp.ake import AuthenticationResult, SRMUpdateCallback, authenticate, open_ake_connection
from pydtcpip.dtcp.crypto import DeviceCredentials
from pydtcpip.dtcp.move import MoveControlConnector, MoveTransaction
from pydtcpip.dtcp.pcp import ProtectedPacket, decrypt_packet, read_packets
from pydtcpip.dtcp.srm import SRM
from pydtcpip.errors import DLNAError, DTCPError
from pydtcpip.events import (
    EventCallback,
    MediaInfoReceived,
    TransferProgressed,
    TransferReceived,
    TransferStarted,
    dispatch_event,
)
from pydtcpip.models import MediaInfo, MoveResumeState, Resource, TimeRange
from pydtcpip.sinks import Sink
from pydtcpip.transfer import (
    ResumeStateCallback,
    abort_move,
    dispatch_transfer_protection,
    finish_move,
    receive_move_body,
)


_LOGGER = logging.getLogger(__name__)

# Async callback for decrypted media chunks (the next read waits until it completes)
type DataCallback = Callable[[bytes], Awaitable[None]]

# Background transfer support bit in the upper 32 bits of the 128-bit DLNA.ORG_FLAGS
_DLNA_FLAG_BACKGROUND_TRANSFER = 0x00400000


class DTCPClient:
    """
    A client that holds sink credentials and receives recordings from DTCP-IP resources.
    It accepts either a Resource from a DLNA listing or a URL string.
    """

    def __init__(
        self,
        credentials: DeviceCredentials,
        *,
        srm: SRM | None = None,
        on_srm_update: SRMUpdateCallback | None = None,
        timeout: float = 30.0,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        """
        Configure sink credentials and SRM.

        Args:
            credentials (DeviceCredentials): Sink device certificate and private key
            srm (SRM | None): Saved SRM (None authenticates with version 0 and receives a newer SRM from the source)
            on_srm_update (SRMUpdateCallback | None): Callback for persisting a newly received SRM
            timeout (float): Authentication and HTTP response timeout in seconds
            http_session (aiohttp.ClientSession | None): Shared HTTP session (created by this client if None)
        """

        self.credentials = credentials
        self.srm = srm if srm is not None else SRM()
        self._on_srm_update = on_srm_update
        self._timeout = timeout
        self._http_session = http_session
        self._owns_http_session = http_session is None

        # Reject a locally revoked certificate before connecting to the source
        self.srm.check_device(credentials.certificate)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the HTTP session created by this client."""

        if self._owns_http_session is True and self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    async def probe(self, target: Resource | str, *, time_range: TimeRange | None = None) -> MediaInfo:
        """
        Obtain transfer information from a media HEAD response, using resource metadata if needed for authentication.

        Args:
            target (Resource | str): Resource or its URL
            time_range (TimeRange | None): Time range to probe

        Returns:
            MediaInfo: Authentication endpoint and transfer information
        """

        url = target.url if isinstance(target, Resource) else target
        resource = target if isinstance(target, Resource) else None
        headers = self._request_headers(time_range=time_range)
        async with self._open('HEAD', url, headers, resource=resource) as (_, media_info):
            return media_info

    async def stream(
        self,
        target: Resource | str,
        on_data: DataCallback,
        *,
        time_range: TimeRange | None = None,
        on_event: EventCallback | None = None,
    ) -> int:
        """
        Authenticate, receive media, and pass decrypted data to on_data in order.
        Streaming does not consume a copy and leaves the source recording in place.

        Args:
            target (Resource | str): Resource or its URL
            on_data (DataCallback): Async callback for decrypted data; the next read waits for it to finish
            time_range (TimeRange | None): Requested range (the source may return a wider range)
            on_event (EventCallback | None): Event callback

        Returns:
            int: Number of bytes passed to on_data
        """

        transfer_id = uuid.uuid4().hex
        url = target.url if isinstance(target, Resource) else target
        resource = target if isinstance(target, Resource) else None
        cleartext_size = resource.cleartext_size if resource is not None else None

        # Obtain the authentication endpoint with HEAD and authenticate before requesting the body
        media_info = await self.probe(target, time_range=time_range)
        await dispatch_event(on_event, MediaInfoReceived(transfer_id=transfer_id, media_info=media_info))
        result = await self._authenticate(media_info, is_move=False, transfer_id=transfer_id, on_event=on_event)
        exchange_key = result.exchange_key

        headers = self._request_headers(time_range=time_range)
        async with self._open('GET', url, headers, resource=resource) as (response, received_info):
            await dispatch_event(
                on_event, MediaInfoReceived(transfer_id=transfer_id, media_info=received_info, method='GET')
            )
            if (received_info.host.lower(), received_info.port) != (media_info.host.lower(), media_info.port):
                raise DTCPError('The HEAD and GET responses specify different DTCP-IP authentication endpoints.')
            await dispatch_event(
                on_event,
                TransferStarted(
                    transfer_id=transfer_id,
                    total_http_bytes=received_info.content_length,
                    total_content_bytes=cleartext_size if time_range is None else None,
                ),
            )

            http_bytes = 0
            content_bytes = 0
            is_first_packet = True
            async for packet in self._read_packets(response):
                if is_first_packet is True:
                    await dispatch_transfer_protection(on_event, transfer_id, packet)
                    is_first_packet = False

                # Reauthenticate to receive a new exchange key when the source rotates it
                if packet.exchange_key_label != exchange_key.label:
                    _LOGGER.info('The source rotated the exchange key; authenticating again.')
                    result = await self._authenticate(
                        media_info, is_move=False, transfer_id=transfer_id, on_event=on_event
                    )
                    exchange_key = result.exchange_key
                    if packet.exchange_key_label != exchange_key.label:
                        raise DTCPError('The exchange key label still does not match after reauthentication.')

                cleartext = decrypt_packet(packet, exchange_key.value)
                http_bytes += len(packet.wire_bytes)
                content_bytes += len(cleartext)
                if len(cleartext) > 0:
                    await on_data(cleartext)
                await dispatch_event(
                    on_event,
                    TransferProgressed(
                        transfer_id=transfer_id,
                        http_bytes=http_bytes,
                        content_bytes=content_bytes,
                        written_bytes=content_bytes,
                    ),
                )

            await dispatch_event(
                on_event, TransferReceived(transfer_id=transfer_id, http_bytes=http_bytes, content_bytes=content_bytes)
            )
            return content_bytes

    async def move(
        self,
        target: Resource | str,
        sink: Sink,
        *,
        on_resume_state: ResumeStateCallback | None = None,
        on_event: EventCallback | None = None,
        reconnect_without_authentication: bool = False,
        finalize_delay: float = 0.0,
    ) -> int:
        """
        Receive a complete recording by GET Move and finalize after the sink persists it.
        This consumes one source copy; if only the final Move remains or copying is prohibited, the source recording is deleted.
        If the finalize or completion response is still missing after retries, the complete data and resume state
        are retained. Use resume_move() to finalize the same transfer.

        Args:
            target (Resource | str): Resource or its URL
            sink (Sink): Destination for decrypted recording data
            on_resume_state (ResumeStateCallback | None): Callback to persist resume state before finalization
            on_event (EventCallback | None): Event callback
            reconnect_without_authentication (bool): Use the saved Move key on a fresh TCP connection without
                another authentication exchange. Enable this for sources that close the connection after authentication.
            finalize_delay (float): Seconds to wait after persisting the recording before requesting finalization,
                for sources that refuse a finalize request sent immediately after the transfer ends

        Returns:
            int: Number of recording bytes written to the sink
        """

        transfer_id = uuid.uuid4().hex
        url = target.url if isinstance(target, Resource) else target
        cleartext_size = target.cleartext_size if isinstance(target, Resource) else None

        # Obtain the endpoint with HEAD and receive a transfer-specific key through Move authentication
        media_info = await self.probe(url)
        await dispatch_event(on_event, MediaInfoReceived(transfer_id=transfer_id, media_info=media_info))
        reader, writer = await open_ake_connection(media_info.host, media_info.port, timeout=self._timeout)
        try:
            result = await self._run_authentication(
                reader,
                writer,
                media_info.host,
                media_info.port,
                is_move=True,
                transfer_id=transfer_id,
                on_event=on_event,
            )
        except BaseException:
            writer.close()
            raise
        transaction = MoveTransaction(
            media_info.host,
            media_info.port,
            result.exchange_key,
            transfer_id=transfer_id,
            timeout=self._timeout,
            connection=(reader, writer),
            connect=self._move_control_connector(
                media_info.host,
                media_info.port,
                transfer_id=transfer_id,
                on_event=on_event,
                reconnect_without_authentication=reconnect_without_authentication,
            ),
            on_event=on_event,
            reconnect_without_authentication=reconnect_without_authentication,
        )

        try:
            # Request the complete Move-encrypted recording with the exchange key label in BLKMove.dtcp.com
            headers = self._request_headers()
            headers['BLKMove.dtcp.com'] = f'{transaction.exchange_key.label:02X}'
            if self._supports_background_transfer(media_info) is True:
                headers['transferMode.dlna.org'] = 'Background'
            async with self._open('GET', url, headers) as (response, received_info):
                await dispatch_event(
                    on_event, MediaInfoReceived(transfer_id=transfer_id, media_info=received_info, method='GET')
                )
                if (received_info.host, received_info.port) != (media_info.host, media_info.port):
                    raise DTCPError('The HEAD and GET responses specify different DTCP-IP authentication endpoints.')

                # Without Content-Length or chunked encoding, connection close cannot distinguish truncation from completion
                ## Receive only if the decrypted size can be checked against metadata; otherwise cancel before finalization
                is_chunked = 'chunked' in response.headers.get('Transfer-Encoding', '').lower()
                is_end_detectable = received_info.content_length is not None or is_chunked
                if is_end_detectable is False and cleartext_size is None:
                    raise DTCPError(
                        'The source did not report the body length, so complete receipt cannot be verified. '
                        'The Move was cancelled to avoid finalizing an incomplete recording.'
                    )
                await dispatch_event(
                    on_event,
                    TransferStarted(
                        transfer_id=transfer_id,
                        total_http_bytes=received_info.content_length,
                        total_content_bytes=cleartext_size,
                    ),
                )
                await receive_move_body(
                    transaction,
                    self._read_packets(response),
                    sink,
                    expected_http_bytes=received_info.content_length,
                    expected_content_bytes=cleartext_size if is_end_detectable is False else None,
                    on_event=on_event,
                )

            async def wait_before_finalize() -> None:
                _LOGGER.info(f'Waiting {finalize_delay:g} seconds for the source before requesting Move finalization.')
                await asyncio.sleep(finalize_delay)

            await finish_move(
                transaction,
                sink,
                on_resume_state=on_resume_state,
                on_event=on_event,
                before_finalize=wait_before_finalize if finalize_delay > 0 else None,
            )
            return transaction.written_bytes

        except BaseException:
            await abort_move(transaction, sink, on_event=on_event)
            raise
        finally:
            await transaction.close()

    async def resume_move(
        self,
        resume_state: MoveResumeState,
        sink: Sink | None,
        *,
        on_resume_state: ResumeStateCallback | None = None,
        on_event: EventCallback | None = None,
        reconnect_without_authentication: bool | None = None,
    ) -> int:
        """
        Resume finalization of a Move whose finalize or completion response was lost after saving the recording.
        Retry with the saved exchange key and nonce without receiving the data again.
        The source's resume state expires, so resume as soon as connectivity is restored.
        Raise MoveCommitmentRejectedError if the source has discarded its resume state.

        Args:
            resume_state (MoveResumeState): Resume state passed to on_resume_state by move()
            sink (Sink | None): Sink for the received data (e.g. FileSink.resume()); None if state is finalized
                and the sink has already been committed
            on_resume_state (ResumeStateCallback | None): Callback for updated resume state
            on_event (EventCallback | None): Event callback
            reconnect_without_authentication (bool | None): Override the saved connection policy, for example
                when restoring an older resume state for a source that closes the connection after authentication

        Returns:
            int: Number of committed recording bytes
        """

        transfer_id = uuid.uuid4().hex
        if reconnect_without_authentication is not None:
            resume_state = resume_state.model_copy(
                update={'reconnect_without_authentication': reconnect_without_authentication}
            )
        transaction = MoveTransaction.from_resume_state(
            resume_state,
            transfer_id=transfer_id,
            timeout=self._timeout,
            connect=self._move_control_connector(
                resume_state.host,
                resume_state.port,
                transfer_id=transfer_id,
                on_event=on_event,
                reconnect_without_authentication=resume_state.reconnect_without_authentication,
            ),
            on_event=on_event,
        )
        try:
            await finish_move(transaction, sink, on_resume_state=on_resume_state, on_event=on_event)
            return transaction.written_bytes
        except BaseException:
            await abort_move(transaction, sink, on_event=on_event)
            raise
        finally:
            await transaction.close()

    def _move_control_connector(
        self,
        host: str,
        port: int,
        *,
        transfer_id: str,
        on_event: EventCallback | None,
        reconnect_without_authentication: bool,
    ) -> MoveControlConnector:
        """
        Create a function that opens a control connection for retrying Move finalization or completion.

        Args:
            host (str): Source authentication address
            port (int): Source authentication port
            transfer_id (str): Transfer identifier for events
            on_event (EventCallback | None): Event callback
            reconnect_without_authentication (bool): Whether to reuse the Move key without another authentication exchange

        Returns:
            MoveControlConnector: Function returning a control connection for finalization retries
        """

        async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            reader, writer = await open_ake_connection(host, port, timeout=self._timeout)
            # Some sources, including DBR-Z620, require authentication on the new control connection
            if reconnect_without_authentication is False:
                try:
                    await self._run_authentication(
                        reader, writer, host, port, is_move=False, transfer_id=transfer_id, on_event=on_event
                    )
                except BaseException:
                    writer.close()
                    raise
            return reader, writer

        return connect

    async def _authenticate(
        self,
        media_info: MediaInfo,
        *,
        is_move: bool,
        transfer_id: str,
        on_event: EventCallback | None,
    ) -> AuthenticationResult:
        """
        Authenticate for streaming and close the control connection afterward.

        Args:
            media_info (MediaInfo): Transfer information including the authentication endpoint
            is_move (bool): Whether to request a Move exchange key
            transfer_id (str): Transfer identifier for events
            on_event (EventCallback | None): Event callback

        Returns:
            AuthenticationResult: Exchange key and updated SRM
        """

        reader, writer = await open_ake_connection(media_info.host, media_info.port, timeout=self._timeout)
        try:
            return await self._run_authentication(
                reader,
                writer,
                media_info.host,
                media_info.port,
                is_move=is_move,
                transfer_id=transfer_id,
                on_event=on_event,
            )
        finally:
            writer.close()

    async def _run_authentication(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
        *,
        is_move: bool,
        transfer_id: str,
        on_event: EventCallback | None,
    ) -> AuthenticationResult:
        """
        Authenticate over an open control connection and update this client's SRM.

        Args:
            reader (asyncio.StreamReader): Control connection reader
            writer (asyncio.StreamWriter): Control connection writer
            host (str): Source authentication address
            port (int): Source authentication port
            is_move (bool): Whether to request a Move exchange key
            transfer_id (str): Transfer identifier for events
            on_event (EventCallback | None): Event callback

        Returns:
            AuthenticationResult: Exchange key and updated SRM
        """

        async with asyncio.timeout(self._timeout):
            result = await authenticate(
                reader,
                writer,
                self.credentials,
                self.srm,
                is_move=is_move,
                transfer_id=transfer_id,
                host=host,
                port=port,
                on_srm_update=self._on_srm_update,
                on_event=on_event,
            )
        self.srm = result.srm
        return result

    def _request_headers(self, *, time_range: TimeRange | None = None) -> dict[str, str]:
        """
        Build DLNA headers for a media HTTP request.

        Args:
            time_range (TimeRange | None): Requested time range

        Returns:
            dict[str, str]: HTTP headers
        """

        headers = {
            'transferMode.dlna.org': 'Streaming',
            'getcontentFeatures.dlna.org': '1',
            # Many recorders do not support compressed responses, so request the uncompressed body
            'Accept-Encoding': 'identity',
        }
        if time_range is not None:
            headers['TimeSeekRange.dlna.org'] = time_range.to_header()
        return headers

    @asynccontextmanager
    async def _open(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        *,
        resource: Resource | None = None,
    ) -> AsyncGenerator[tuple[aiohttp.ClientResponse, MediaInfo]]:
        """
        Send a media HTTP request and validate its DTCP-IP response and authentication endpoint.

        Args:
            method (str): HEAD or GET
            url (str): Resource URL
            headers (dict[str, str]): HTTP headers
            resource (Resource | None): Advertised recording resource eligible for endpoint fallback

        Yields:
            tuple[aiohttp.ClientResponse, MediaInfo]: Response before reading its body and parsed media information
        """

        if self._http_session is None:
            self._http_session = aiohttp.ClientSession()
        timeout = aiohttp.ClientTimeout(total=None, connect=self._timeout, sock_read=self._timeout)
        try:
            async with self._http_session.request(
                method,
                url,
                headers=headers,
                timeout=timeout,
                auto_decompress=False,
                allow_redirects=False,
            ) as response:
                # Include HTTP status in errors; the caller interprets device-specific meanings such as client refusal
                if response.status >= 400:
                    raise DLNAError(
                        f'Media {method} request failed (HTTP {response.status}).',
                        http_status=response.status,
                        # Preserve diagnostic fields without copying cookies or authentication headers into exceptions
                        diagnostic_headers={
                            name: response.headers[name]
                            for name in ('error-info', 'Error.pxn.panasonic.com')
                            if name in response.headers
                        },
                    )

                # Some recorders return a media Content-Type without the advertised DTCP-IP wrapper
                content_type, parameters = parse_content_type(response.headers.get('Content-Type', ''))
                content_format = parameters.get('CONTENTFORMAT')
                host = parameters.get('DTCP1HOST', '')
                port = parameters.get('DTCP1PORT', '')

                if content_type != 'application/x-dtcp1':
                    if resource is None or not resource.protocol_info.is_dtcp:
                        raise DTCPError(f'The stream is not protected by DTCP-IP (Content-Type: {content_type}).')
                    if response.status not in (200, 206):
                        raise DLNAError(
                            f'Media {method} request did not return a recording response (HTTP {response.status}).',
                            http_status=response.status,
                        )

                    xml_parameters = resource.protocol_info.content_type_parameters
                    xml_host = xml_parameters.get('DTCP1HOST', '')
                    xml_port = xml_parameters.get('DTCP1PORT', '')
                    parsed_url = urlsplit(url)

                    # The fallback must use an authentication endpoint on the same host as the resource URL
                    if (
                        parsed_url.scheme not in ('http', 'https')
                        or parsed_url.hostname is None
                        or xml_host == ''
                        or xml_host.lower() != parsed_url.hostname.lower()
                        or not xml_port.isdecimal()
                        or not 1 <= int(xml_port) <= 65535
                    ):
                        raise DTCPError(
                            'The Resource DTCP-IP authentication endpoint is missing, invalid, '
                            'or does not match the resource host.'
                        )

                    host = xml_host
                    port = xml_port
                    content_format = xml_parameters.get('CONTENTFORMAT')
                elif host == '' or port.isdecimal() is False or not 1 <= int(port) <= 65535:
                    raise DTCPError('The HTTP response has no valid DTCP-IP authentication endpoint.')

                time_range = response.headers.get('TimeSeekRange.dlna.org')
                if 'TimeSeekRange.dlna.org' in headers and time_range is None:
                    raise DLNAError('The source did not return a response for the requested time range.')
                content_length = response.headers.get('Content-Length')
                media_info = MediaInfo(
                    url=url,
                    host=host,
                    port=int(port),
                    content_type=content_type,
                    content_format=content_format,
                    content_features=response.headers.get('contentFeatures.dlna.org'),
                    content_length=int(content_length)
                    if content_length is not None and content_length.isdecimal()
                    else None,
                    time_range=time_range,
                    headers=dict(response.headers),
                )
                yield response, media_info
        except aiohttp.ClientError as ex:
            raise DLNAError(f'Media {method} request failed ({ex}).') from ex

    async def _read_packets(self, response: aiohttp.ClientResponse) -> AsyncIterator[ProtectedPacket]:
        """
        Read protected packets from the HTTP body and raise DTCPError if the body is truncated.
        aiohttp reports incomplete Content-Length or chunked bodies as ClientPayloadError.

        Args:
            response (aiohttp.ClientResponse): Response before reading the body

        Yields:
            ProtectedPacket: Protected packet read from the body
        """

        try:
            async for packet in read_packets(response.content):
                yield packet
        except aiohttp.ClientPayloadError as ex:
            raise DTCPError('The media HTTP body ended before all data arrived.') from ex

    def _supports_background_transfer(self, media_info: MediaInfo) -> bool:
        """
        Check background transfer support using DLNA.ORG_FLAGS in contentFeatures.

        Args:
            media_info (MediaInfo): Media information from the HEAD response

        Returns:
            bool: True if background transfer is supported
        """

        for feature in (media_info.content_features or '').split(';'):
            if feature.startswith('DLNA.ORG_FLAGS=') is False:
                continue
            # DLNA.ORG_FLAGS has 32 hexadecimal digits; the first 8 contain the primary flags
            primary_flags = feature.partition('=')[2][:8]
            try:
                return int(primary_flags, 16) & _DLNA_FLAG_BACKGROUND_TRANSFER != 0
            except ValueError:
                return False
        return False
