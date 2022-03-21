# Released under the MIT License. See LICENSE for details.
#
"""Remote procedure call related functionality."""

from __future__ import annotations

import ssl
import time
import asyncio
import logging
import weakref
from enum import Enum
from dataclasses import dataclass
from threading import current_thread
from typing import TYPE_CHECKING, Annotated

from efro.error import CommunicationError
from efro.util import assert_never
from efro.dataclassio import (dataclass_to_json, dataclass_from_json,
                              ioprepped, IOAttrs)

if TYPE_CHECKING:
    from typing import Literal, Awaitable, Callable, Optional

# Terminology:
# Packet: A chunk of data consisting of a type and some type-dependent
#         payload. Even though we use streams we organize our transmission
#         into 'packets'.
# Message: User data which we transmit using one or more packets.


class _PacketType(Enum):
    HANDSHAKE = 0
    KEEPALIVE = 1
    MESSAGE = 2
    RESPONSE = 3


_BYTE_ORDER: Literal['big'] = 'big'


@ioprepped
@dataclass
class _PeerInfo:

    # So we can gracefully evolve how we communicate in the future.
    protocol: Annotated[int, IOAttrs('p')]

    # How often we'll be sending out keepalives (in seconds).
    keepalive_interval: Annotated[float, IOAttrs('k')]


OUR_PROTOCOL = 1


class _InFlightMessage:
    """Represents a message that is out on the wire."""

    def __init__(self) -> None:
        self._response: Optional[bytes] = None
        self._got_response = asyncio.Event()
        self.wait_task = asyncio.create_task(self._wait())

    async def _wait(self) -> bytes:
        await self._got_response.wait()
        assert self._response is not None
        return self._response

    def set_response(self, data: bytes) -> None:
        """Set response data."""
        assert self._response is None
        self._response = data
        self._got_response.set()


class _KeepaliveTimeoutError(Exception):
    """Raised if we time out due to not receiving keepalives."""


class RPCEndpoint:
    """Facilitates asynchronous multiplexed remote procedure calls.

    Be aware that, while multiple calls can be in flight in either direction
    simultaneously, packets are still sent serially in a single
    stream. So excessively long messages/responses will delay all other
    communication. If/when this becomes an issue we can look into breaking up
    long messages into multiple packets.
    """

    # Set to True on an instance to test keepalive failures.
    test_suppress_keepalives: bool = False

    # How long we should wait before giving up on a message by default.
    # Note this includes processing time on the other end.
    DEFAULT_MESSAGE_TIMEOUT = 60.0

    # How often we send out keepalive packets by default.
    DEFAULT_KEEPALIVE_INTERVAL = 10.73  # (avoid too regular of values)

    # How long we can go without receiving a keepalive packet before we
    # disconnect.
    DEFAULT_KEEPALIVE_TIMEOUT = 30.0

    def __init__(self,
                 handle_raw_message_call: Callable[[bytes], Awaitable[bytes]],
                 reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter,
                 debug_print: bool,
                 label: str,
                 keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
                 keepalive_timeout: float = DEFAULT_KEEPALIVE_TIMEOUT) -> None:
        self._handle_raw_message_call = handle_raw_message_call
        self._reader = reader
        self._writer = writer
        self._debug_print = debug_print
        self._label = label
        self._thread = current_thread()
        self._closing = False
        self._did_wait_closed = False
        self._event_loop = asyncio.get_running_loop()
        self._out_packets: list[bytes] = []
        self._have_out_packets = asyncio.Event()
        self._run_called = False
        self._peer_info: Optional[_PeerInfo] = None
        self._keepalive_interval = keepalive_interval
        self._keepalive_timeout = keepalive_timeout

        # Need to hold weak-refs to these otherwise it creates dep-loops
        # which keeps us alive.
        self._tasks: list[weakref.ref[asyncio.Task]] = []

        # When we last got a keepalive or equivalent (time.monotonic value)
        self._last_keepalive_receive_time: Optional[float] = None

        # (Start near the end to make sure our looping logic is sound).
        self._next_message_id = 65530

        self._in_flight_messages: dict[int, _InFlightMessage] = {}

        if self._debug_print:
            peername = self._writer.get_extra_info('peername')
            print(f'{self._label}: connected to {peername} at {self._tm()}.')

    async def run(self) -> None:
        """Run the endpoint until the connection is lost or closed.

        Handles closing the provided reader/writer on close.
        """
        self._check_env()

        if self._run_called:
            raise RuntimeError('Run can be called only once per endpoint.')
        self._run_called = True

        core_tasks = [
            asyncio.create_task(
                self._run_core_task('keepalive', self._run_keepalive_task())),
            asyncio.create_task(
                self._run_core_task('read', self._run_read_task())),
            asyncio.create_task(
                self._run_core_task('write', self._run_write_task()))
        ]
        self._tasks += [weakref.ref(t) for t in core_tasks]

        # Run our core tasks until they all complete.
        results = await asyncio.gather(*core_tasks, return_exceptions=True)

        # Core tasks should handle their own errors; the only ones
        # we expect to bubble up are CancelledError.
        for result in results:
            # We want to know if any errors happened aside from CancelledError
            # (which are BaseExceptions, not Exception).
            if isinstance(result, Exception):
                if self._debug_print:
                    logging.error('Got unexpected error from %s core task: %s',
                                  self._label, result)

        # Shut ourself down.
        try:
            self.close()
            await self.wait_closed()
        except Exception:
            logging.exception('Error closing %s.', self._label)

        if self._debug_print:
            print(f'{self._label}: finished.')

    async def send_message(self,
                           message: bytes,
                           timeout: Optional[float] = None) -> bytes:
        """Send a message to the peer and return a response.

        If timeout is not provided, the default will be used.
        Raises a CommunicationError if the round trip is not completed
        for any reason.
        """
        self._check_env()
        if len(message) > 65535:
            raise RuntimeError('Message cannot be larger than 65535 bytes')

        if self._closing:
            raise CommunicationError('Endpoint is closed')

        # Go with 16 bit looping value for message_id.
        message_id = self._next_message_id
        self._next_message_id = (self._next_message_id + 1) % 65536

        # Payload consists of type (1b), message_id (2b), len (2b), and data.
        self._enqueue_outgoing_packet(
            _PacketType.MESSAGE.value.to_bytes(1, _BYTE_ORDER) +
            message_id.to_bytes(2, _BYTE_ORDER) +
            len(message).to_bytes(2, _BYTE_ORDER) + message)

        # Make an entry so we know this message is out there.
        assert message_id not in self._in_flight_messages
        msgobj = self._in_flight_messages[message_id] = _InFlightMessage()

        # Also add its task to our list so we properly cancel it if we die.
        self._prune_tasks()  # Keep our list from filling with dead tasks.
        self._tasks.append(weakref.ref(msgobj.wait_task))

        # Note: we always want to incorporate a timeout. Individual
        # messages may hang or error on the other end and this ensures
        # we won't build up lots of zombie tasks waiting around for
        # responses that will never arrive.
        if timeout is None:
            timeout = self.DEFAULT_MESSAGE_TIMEOUT
        assert timeout is not None
        try:
            return await asyncio.wait_for(msgobj.wait_task, timeout=timeout)
        except asyncio.CancelledError as exc:
            if self._debug_print:
                print(f'{self._label}: message {message_id} was cancelled.')
            raise CommunicationError() from exc
        except asyncio.TimeoutError as exc:
            if self._debug_print:
                print(f'{self._label}: message {message_id} timed out.')

            # Stop waiting on the response.
            msgobj.wait_task.cancel()

            # Remove the record of this message.
            del self._in_flight_messages[message_id]

            # Let the user know something went wrong.
            raise CommunicationError() from exc

    def close(self) -> None:
        """I said seagulls; mmmm; stop it now."""
        self._check_env()

        if self._closing:
            return

        if self._debug_print:
            print(f'{self._label}: closing...')

        self._closing = True

        # Kill all of our in-flight tasks.
        if self._debug_print:
            print(f'{self._label}: cancelling tasks...')
        for task in self._get_live_tasks():
            task.cancel()

        if self._debug_print:
            print(f'{self._label}: closing writer...')
        self._writer.close()

        # We don't need this anymore and it is likely to be creating a
        # dependency loop.
        del self._handle_raw_message_call

    def is_closing(self) -> bool:
        """Have we begun the process of closing?"""
        return self._closing

    async def wait_closed(self) -> None:
        """I said seagulls; mmmm; stop it now."""
        self._check_env()

        # Make sure we only *enter* this call once.
        if self._did_wait_closed:
            return
        self._did_wait_closed = True

        if not self._closing:
            raise RuntimeError('Must be called after close()')

        if self._debug_print:
            print(f'{self._label}: waiting for close to complete...')

        # Wait for all of our in-flight tasks to wrap up.
        results = await asyncio.gather(*self._get_live_tasks(),
                                       return_exceptions=True)
        for result in results:
            # We want to know if any errors happened aside from CancelledError
            # (which are BaseExceptions, not Exception).
            if isinstance(result, Exception):
                if self._debug_print:
                    logging.error(
                        'Got unexpected error cleaning up %s task: %s',
                        self._label, result)

        # At this point we shouldn't touch our tasks anymore.
        # Clearing them out allows us to go down
        # del self._tasks

        # Now wait for our writer to finish going down.
        # When we close our writer it generally triggers errors
        # in our current blocked read/writes. However that same
        # error is also sometimes returned from _writer.wait_closed().
        # See connection_lost() in asyncio/streams.py to see why.
        # So let's silently ignore it when that happens.
        assert self._writer.is_closing()
        try:
            await self._writer.wait_closed()
        except Exception as exc:
            if not self._is_expected_connection_error(exc):
                logging.exception('Error closing _writer for %s.', self._label)
            else:
                if self._debug_print:
                    print(f'{self._label}: silently ignoring error in'
                          f' _writer.wait_closed(): {exc}.')

    def _tm(self) -> str:
        """Simple readable time value for debugging."""
        tval = time.time() % 100.0
        return f'{tval:.2f}'

    async def _run_read_task(self) -> None:
        """Read from the peer."""
        self._check_env()
        assert self._peer_info is None

        # The first thing they should send us is their handshake; then
        # we'll know if/how we can talk to them.
        mlen = await self._read_int_32()
        message = (await self._reader.readexactly(mlen))
        self._peer_info = dataclass_from_json(_PeerInfo, message.decode())
        self._last_keepalive_receive_time = time.monotonic()
        if self._debug_print:
            print(f'{self._label}: received handshake at {self._tm()}.')

        # Now just sit and handle stuff as it comes in.
        while True:
            assert not self._closing

            # Read message type.
            mtype = _PacketType(await self._read_int_8())
            if mtype is _PacketType.HANDSHAKE:
                raise RuntimeError('Got multiple handshakes')

            if mtype is _PacketType.KEEPALIVE:
                if self._debug_print:
                    print(f'{self._label}: received keepalive'
                          f' at {self._tm()}.')
                self._last_keepalive_receive_time = time.monotonic()

            elif mtype is _PacketType.MESSAGE:
                await self._handle_message_packet()

            elif mtype is _PacketType.RESPONSE:
                await self._handle_response_packet()

            else:
                assert_never(mtype)

    async def _handle_message_packet(self) -> None:
        msgid = await self._read_int_16()
        msglen = await self._read_int_16()
        msg = await self._reader.readexactly(msglen)
        if self._debug_print:
            print(f'{self._label}: received message {msgid}'
                  f' of size {msglen} at {self._tm()}.')

        # Create a message-task to handle this message and return
        # a response (we don't want to block while that happens).
        assert not self._closing
        self._prune_tasks()  # Keep from filling with dead tasks.
        self._tasks.append(
            weakref.ref(
                asyncio.create_task(
                    self._handle_raw_message(message_id=msgid, message=msg))))
        print(f'{self._label}: done handling message at {self._tm()}.')

    async def _handle_response_packet(self) -> None:
        msgid = await self._read_int_16()
        rsplen = await self._read_int_16()
        if self._debug_print:
            print(f'{self._label}: received response {msgid}'
                  f' of size {rsplen} at {self._tm()}.')
        rsp = await self._reader.readexactly(rsplen)
        msgobj = self._in_flight_messages.get(msgid)
        if msgobj is None:
            # It's possible for us to get a response to a message
            # that has timed out. In this case we will have no local
            # record of it.
            if self._debug_print:
                print(f'{self._label}: got response for nonexistent'
                      f' message id {msgid}; perhaps it timed out?')
        else:
            msgobj.set_response(rsp)

    async def _run_write_task(self) -> None:
        """Write to the peer."""

        self._check_env()

        # Introduce ourself so our peer knows how it can talk to us.
        data = dataclass_to_json(
            _PeerInfo(protocol=OUR_PROTOCOL,
                      keepalive_interval=self._keepalive_interval)).encode()
        self._writer.write(len(data).to_bytes(4, _BYTE_ORDER) + data)

        # Now just write out-messages as they come in.
        while True:

            # Wait until some data comes in.
            await self._have_out_packets.wait()

            assert self._out_packets
            data = self._out_packets.pop(0)

            # Important: only clear this once all packets are sent.
            if not self._out_packets:
                self._have_out_packets.clear()

            self._writer.write(data)
            # await self._writer.drain()

    async def _run_keepalive_task(self) -> None:
        """Send periodic keepalive packets."""
        self._check_env()

        # We explicitly send our own keepalive packets so we can stay
        # more on top of the connection state and possibly decide to
        # kill it when contact is lost more quickly than the OS would
        # do itself (or at least keep the user informed that the
        # connection is lagging). It sounds like we could have the TCP
        # layer do this sort of thing itself but that might be
        # OS-specific so gonna go this way for now.
        while True:
            assert not self._closing
            await asyncio.sleep(self._keepalive_interval)
            if not self.test_suppress_keepalives:
                self._enqueue_outgoing_packet(
                    _PacketType.KEEPALIVE.value.to_bytes(1, _BYTE_ORDER))

            # Also go ahead and handle dropping the connection if we
            # haven't heard from the peer in a while.
            # NOTE: perhaps we want to do something more exact than
            # this which only checks once per keepalive-interval?..
            now = time.monotonic()
            assert self._peer_info is not None

            if (self._last_keepalive_receive_time is not None
                    and now - self._last_keepalive_receive_time >
                    self._keepalive_timeout):
                if self._debug_print:
                    since = now - self._last_keepalive_receive_time
                    print(f'{self._label}: reached keepalive time-out'
                          f' ({since:.1f}s).')
                raise _KeepaliveTimeoutError()

    async def _run_core_task(self, tasklabel: str, call: Awaitable) -> None:
        try:
            await call
        except Exception as exc:
            # We expect connection errors to put us here, but make noise
            # if something else does.
            if not self._is_expected_connection_error(exc):
                logging.exception('Unexpected error in rpc %s %s task.',
                                  self._label, tasklabel)
            else:
                if self._debug_print:
                    print(f'{self._label}: {tasklabel} task will exit cleanly'
                          f' due to {exc!r}.')
        finally:
            # Any core task exiting triggers shutdown.
            if self._debug_print:
                print(f'{self._label}: {tasklabel} task exiting...')
            self.close()

    async def _handle_raw_message(self, message_id: int,
                                  message: bytes) -> None:
        try:
            response = await self._handle_raw_message_call(message)
        except Exception:
            # We expect local message handler to always succeed.
            # If that doesn't happen, make a fuss so we know to fix it.
            # The other end will simply never get a response to this
            # message.
            logging.exception('Error handling message')
            return

        # Now send back our response.
        # Payload consists of type (1b), msgid (2b), len (2b), and data.
        self._enqueue_outgoing_packet(
            _PacketType.RESPONSE.value.to_bytes(1, _BYTE_ORDER) +
            message_id.to_bytes(2, _BYTE_ORDER) +
            len(response).to_bytes(2, _BYTE_ORDER) + response)

    async def _read_int_8(self) -> int:
        return int.from_bytes(await self._reader.readexactly(1), _BYTE_ORDER)

    async def _read_int_16(self) -> int:
        return int.from_bytes(await self._reader.readexactly(2), _BYTE_ORDER)

    async def _read_int_32(self) -> int:
        return int.from_bytes(await self._reader.readexactly(4), _BYTE_ORDER)

    @classmethod
    def _is_expected_connection_error(cls, exc: Exception) -> bool:

        # We expect this stuff to be what ends us.
        if isinstance(exc, (
                ConnectionError,
                EOFError,
                _KeepaliveTimeoutError,
        )):
            return True

        # Am occasionally getting a specific SSL error on shutdown which I
        # believe is harmless (APPLICATION_DATA_AFTER_CLOSE_NOTIFY).
        # It sounds like it may soon be ignored by Python (as of March 2022).
        # Let's still complain, however, if we get any SSL errors besides
        # this one. https://bugs.python.org/issue39951
        if isinstance(exc, ssl.SSLError):
            if 'APPLICATION_DATA_AFTER_CLOSE_NOTIFY' in str(exc):
                return True

        return False

    def _check_env(self) -> None:
        # I was seeing that asyncio stuff wasn't working as expected if
        # created in one thread and used in another, so let's enforce
        # a single thread for all use of an instance.
        if current_thread() is not self._thread:
            raise RuntimeError('This must be called from the same thread'
                               ' that the endpoint was created in.')

        # This should always be the case if thread is the same.
        assert asyncio.get_running_loop() is self._event_loop

    def _enqueue_outgoing_packet(self, data: bytes) -> None:
        """Enqueue a raw packet to be sent. Must be called from our loop."""
        self._check_env()

        if bool(True):
            if self._debug_print:
                print(f'{self._label}: enqueueing outgoing packet'
                      f' {data[:50]!r} at {self._tm()}.')

        # Add the data and let our write task know about it.
        self._out_packets.append(data)
        self._have_out_packets.set()

    def _prune_tasks(self) -> None:
        out: list[weakref.ref[asyncio.Task]] = []
        for task_weak_ref in self._tasks:
            task = task_weak_ref()
            if task is not None and not task.done():
                out.append(task_weak_ref)
        self._tasks = out

    def _get_live_tasks(self) -> list[asyncio.Task]:
        out: list[asyncio.Task] = []
        for task_weak_ref in self._tasks:
            task = task_weak_ref()
            if task is not None and not task.done():
                out.append(task)
        return out
