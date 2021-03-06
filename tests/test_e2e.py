import asyncio
import functools
import logging
import os
import pathlib
import ssl
import socket
import threading
import unittest
import unittest.mock

import OpenSSL.SSL

import aioopenssl


PORT = int(os.environ.get("AIOOPENSSL_TEST_PORT", "12345"))
KEYFILE = pathlib.Path(__file__).parent / "ssl.pem"


def blocking(meth):
    @functools.wraps(meth)
    def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(
            asyncio.wait_for(meth(*args, **kwargs), 1)
        )

    return wrapper


class TestSSLConnection(unittest.TestCase):
    TRY_PORTS = list(range(10000, 10010))

    @blocking
    async def setUp(self):
        self.loop = asyncio.get_event_loop()
        self.server = None
        self.server_ctx = ssl.create_default_context(
            ssl.Purpose.CLIENT_AUTH
        )

        self.server_ctx.load_cert_chain(str(KEYFILE))
        await self._replace_server()

        self.inbound_queue = asyncio.Queue()

    async def _shutdown_server(self):
        self.server.close()
        while not self.inbound_queue.empty():
            reader, writer = await self.inbound_queue.get()
            writer.close()
        await self.server.wait_closed()
        self.server = None

    async def _replace_server(self):
        if self.server is not None:
            await self._shutdown_server()

        self.server = await asyncio.start_server(
            self._server_accept,
            host="127.0.0.1",
            port=PORT,
            ssl=self.server_ctx,
        )

    @blocking
    async def tearDown(self):
        await self._shutdown_server()

    def _server_accept(self, reader, writer):
        self.inbound_queue.put_nowait(
            (reader, writer)
        )

    def _stream_reader_proto(self):
        reader = asyncio.StreamReader(loop=self.loop)
        proto = asyncio.StreamReaderProtocol(reader)
        proto.aioopenssl_test_reader = reader
        return proto

    async def _connect(self, *args, **kwargs):
        transport, reader_proto = \
            await aioopenssl.create_starttls_connection(
                asyncio.get_event_loop(),
                self._stream_reader_proto,
                *args,
                **kwargs
            )
        reader = reader_proto.aioopenssl_test_reader
        del reader_proto.aioopenssl_test_reader
        writer = asyncio.StreamWriter(transport, reader_proto, reader,
                                      self.loop)
        return transport, reader, writer

    @blocking
    async def test_send_and_receive_data(self):
        _, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=False,
        )

        s_reader, s_writer = await self.inbound_queue.get()

        c_writer.write(b"foobar")
        s_writer.write(b"fnord")

        await asyncio.gather(s_writer.drain(), c_writer.drain())

        c_read, s_read = await asyncio.gather(
            c_reader.readexactly(5),
            s_reader.readexactly(6),
        )

        self.assertEqual(
            s_read,
            b"foobar"
        )

        self.assertEqual(
            c_read,
            b"fnord"
        )

    @blocking
    async def test_send_large_data(self):
        _, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=False,
        )

        s_reader, s_writer = await self.inbound_queue.get()

        data = bytearray(2**17)

        c_writer.write(data)
        s_writer.write(b"foobar")

        await asyncio.gather(s_writer.drain(), c_writer.drain())

        c_read, s_read = await asyncio.gather(
            c_reader.readexactly(6),
            s_reader.readexactly(len(data)),
        )

        self.assertEqual(
            s_read,
            data,
        )

        self.assertEqual(
            c_read,
            b"foobar",
        )

    @blocking
    async def test_recv_large_data(self):
        _, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=False,
        )

        s_reader, s_writer = await self.inbound_queue.get()

        data = bytearray(2**17)

        s_writer.write(data)
        c_writer.write(b"foobar")

        await asyncio.gather(s_writer.drain(), c_writer.drain())

        c_read, s_read = await asyncio.gather(
            c_reader.readexactly(len(data)),
            s_reader.readexactly(6),
        )

        self.assertEqual(
            c_read,
            data,
        )

        self.assertEqual(
            s_read,
            b"foobar",
        )

    @blocking
    async def test_send_recv_large_data(self):
        _, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=False,
        )

        s_reader, s_writer = await self.inbound_queue.get()

        data1 = bytearray(2**17)
        data2 = bytearray(2**17)

        s_writer.write(data1)
        c_writer.write(data2)

        await asyncio.gather(s_writer.drain(), c_writer.drain())

        c_read, s_read = await asyncio.gather(
            c_reader.readexactly(len(data1)),
            s_reader.readexactly(len(data2)),
        )

        self.assertEqual(
            c_read,
            data1,
        )

        self.assertEqual(
            s_read,
            data2,
        )

    @blocking
    async def test_abort(self):
        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=False,
        )

        s_reader, s_writer = await self.inbound_queue.get()

        c_transport.abort()

        with self.assertRaises(ConnectionError):
            await asyncio.gather(c_writer.drain())

    @blocking
    async def test_local_addr(self):
        last_exc = None
        used_port = None

        for port in self.TRY_PORTS:
            try:
                c_transport, c_reader, c_writer = await self._connect(
                    host="127.0.0.1",
                    port=PORT,
                    ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                        OpenSSL.SSL.SSLv23_METHOD
                    ),
                    server_hostname="localhost",
                    use_starttls=False,
                    local_addr=("127.0.0.1", port)
                )
            except OSError as exc:
                last_exc = exc
                continue
            used_port = port
            break
        else:
            raise last_exc

        s_reader, s_writer = await self.inbound_queue.get()
        sock = s_writer.transport.get_extra_info("socket")
        peer_addr = sock.getpeername()

        self.assertEqual(peer_addr, ("127.0.0.1", used_port))

    @blocking
    async def test_starttls(self):
        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=True,
        )

        await c_transport.starttls()

        s_reader, s_writer = await self.inbound_queue.get()

        c_writer.write(b"foobar")
        s_writer.write(b"fnord")

        await asyncio.gather(s_writer.drain(), c_writer.drain())

        c_read, s_read = await asyncio.gather(
            c_reader.readexactly(5),
            s_reader.readexactly(6),
        )

        self.assertEqual(
            s_read,
            b"foobar"
        )

        self.assertEqual(
            c_read,
            b"fnord"
        )

    @blocking
    async def test_renegotiation(self):
        self.server_ctx = ssl.create_default_context(
            ssl.Purpose.CLIENT_AUTH
        )
        if hasattr(ssl, "OP_NO_TLSv1_3"):
            # Need to forbid TLS v1.3, since TLSv1.3+ does not support
            # renegotiation
            self.server_ctx.options |= ssl.OP_NO_TLSv1_3

        self.server_ctx.load_cert_chain(str(KEYFILE))
        await self._replace_server()

        def factory(_):
            ctx = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)
            if hasattr(OpenSSL.SSL, "OP_NO_TLSv1_3"):
                # Need to forbid TLS v1.3, since TLSv1.3+ does not support
                # renegotiation
                ctx.set_options(OpenSSL.SSL.OP_NO_TLSv1_3)
            return ctx

        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=factory,
            server_hostname="localhost",
            use_starttls=False,
        )

        s_reader, s_writer = await self.inbound_queue.get()
        ssl_sock = c_transport.get_extra_info("ssl_object")

        c_writer.write(b"foobar")
        s_writer.write(b"fnord")

        await asyncio.gather(s_writer.drain(), c_writer.drain())

        c_read, s_read = await asyncio.gather(
            c_reader.readexactly(5),
            s_reader.readexactly(6),
        )

        self.assertEqual(
            s_read,
            b"foobar"
        )

        self.assertEqual(
            c_read,
            b"fnord"
        )

        ssl_sock.renegotiate()

    @blocking
    async def test_post_handshake_exception_is_propagated(self):
        class FooException(Exception):
            pass

        async def post_handshake_callback(transport):
            raise FooException()

        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=True,
            post_handshake_callback=post_handshake_callback,
        )

        with self.assertRaises(FooException):
            await c_transport.starttls()

    @blocking
    async def test_no_data_is_sent_if_handshake_crashes(self):
        class FooException(Exception):
            pass

        async def post_handshake_callback(transport):
            await asyncio.sleep(0.5)
            raise FooException()

        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=True,
            post_handshake_callback=post_handshake_callback,
        )

        starttls_task = asyncio.ensure_future(c_transport.starttls())
        # ensure that handshake is in progress...
        await asyncio.sleep(0.2)
        c_transport.write(b"foobar")

        with self.assertRaises(FooException):
            await starttls_task

        s_reader, s_writer = await self.inbound_queue.get()

        with self.assertRaises(Exception) as ctx:
            await asyncio.wait_for(
                s_reader.readexactly(6),
                timeout=0.1,
            )

        exc = ctx.exception
        # using type(None) as default, since that will always be false in the
        # isinstance check below
        incomplete_read_exc_type = getattr(
            asyncio.streams, "IncompleteReadError",
            getattr(asyncio, "IncompleteReadError", type(None))
        )
        if isinstance(exc, incomplete_read_exc_type):
            self.assertFalse(exc.partial)
        elif not isinstance(exc, ConnectionResetError):
            raise exc

    @blocking
    async def test_no_data_is_received_if_handshake_crashes(self):
        class FooException(Exception):
            pass

        async def post_handshake_callback(transport):
            await asyncio.sleep(0.5)
            raise FooException()

        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=True,
            post_handshake_callback=post_handshake_callback,
        )

        starttls_task = asyncio.ensure_future(c_transport.starttls())
        s_reader, s_writer = await self.inbound_queue.get()
        self.assertFalse(starttls_task.done())
        s_writer.write(b"fnord")

        with self.assertRaises(FooException):
            await c_reader.readexactly(5)

        with self.assertRaises(FooException):
            await starttls_task

    @blocking
    async def test_data_is_sent_after_handshake(self):
        async def post_handshake_callback(transport):
            await asyncio.sleep(0.5)

        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=True,
            post_handshake_callback=post_handshake_callback,
        )

        starttls_task = asyncio.ensure_future(c_transport.starttls())
        # ensure that handshake is in progress...
        await asyncio.sleep(0.2)
        c_transport.write(b"foobar")

        await starttls_task

        s_reader, s_writer = await self.inbound_queue.get()

        s_recv = await asyncio.wait_for(
            s_reader.readexactly(6),
            timeout=0.1,
        )

        self.assertEqual(s_recv, b"foobar")

    @blocking
    async def test_no_data_is_received_after_handshake(self):
        async def post_handshake_callback(transport):
            await asyncio.sleep(0.5)

        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=True,
            post_handshake_callback=post_handshake_callback,
        )

        starttls_task = asyncio.ensure_future(c_transport.starttls())
        s_reader, s_writer = await self.inbound_queue.get()
        self.assertFalse(starttls_task.done())
        s_writer.write(b"fnord")

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                c_reader.readexactly(5),
                timeout=0.1,
            )

        await starttls_task

        c_recv = await c_reader.readexactly(5)

        self.assertEqual(c_recv, b"fnord")

    @blocking
    async def test_close_during_handshake(self):
        cancelled = None

        async def post_handshake_callback(transport):
            nonlocal cancelled
            try:
                await asyncio.sleep(0.5)
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True

        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=True,
            post_handshake_callback=post_handshake_callback,
        )

        starttls_task = asyncio.ensure_future(c_transport.starttls())
        # ensure that handshake is in progress...
        await asyncio.sleep(0.2)
        c_transport.close()

        with self.assertRaises(ConnectionError):
            await starttls_task

        self.assertTrue(cancelled)


class ServerThread(threading.Thread):
    def __init__(self, ctx, port, loop, queue):
        super().__init__()
        self._logger = logging.getLogger("ServerThread")
        self._ctx = ctx
        self._socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
            0,
        )
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", port))
        self._socket.settimeout(0.5)
        self._socket.listen(0)
        self._loop = loop
        self._queue = queue
        self.stopped = False

    def _push(self, arg):
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait,
            arg,
        )

    def run(self):
        self._logger.info("ready")
        while not self.stopped:
            try:
                client, addr = self._socket.accept()
            except socket.timeout:
                self._logger.debug("no connection yet, cycling")
                continue

            self._logger.debug("connection accepted from %s", addr)

            try:
                wrapped = OpenSSL.SSL.Connection(self._ctx, client)
                wrapped.set_accept_state()
                wrapped.do_handshake()
            except Exception as exc:
                try:
                    wrapped.close()
                except:  # NOQA
                    pass
                try:
                    client.shutdown(socket.SHUT_RDWR)
                    client.close()
                except:  # NOQA
                    pass
                self._push((False, exc))
            else:
                self._push((True, wrapped))

        self._logger.info("shutting down")
        self._socket.shutdown(socket.SHUT_RDWR)
        self._socket.close()


class TestSSLConnectionThreadServer(unittest.TestCase):
    TRY_PORTS = list(range(10000, 10010))

    @blocking
    async def setUp(self):
        self.loop = asyncio.get_event_loop()
        self.thread = None

        ctx = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)
        ctx.use_certificate_chain_file(str(KEYFILE))
        ctx.use_privatekey_file(str(KEYFILE))

        self._replace_thread(ctx)

    def _replace_thread(self, ctx):
        if self.thread is not None:
            self.thread.stopped = True
            self.thread.join()
            self.thread = None

        self.inbound_queue = asyncio.Queue()
        self.thread = ServerThread(
            ctx,
            PORT+1,
            self.loop,
            self.inbound_queue,
        )
        self.thread.start()

    @blocking
    async def tearDown(self):
        self.thread.stopped = True
        self.thread.join()

    async def _get_inbound(self):
        ok, data = await self.inbound_queue.get()
        if not ok:
            raise data
        return data

    async def recv_thread(self, sock, *argv):
        return await self.loop.run_in_executor(
            None,
            sock.recv,
            *argv
        )

    async def send_thread(self, sock, *argv):
        return await self.loop.run_in_executor(
            None,
            sock.send,
            *argv
        )

    def _stream_reader_proto(self):
        reader = asyncio.StreamReader(loop=self.loop)
        proto = asyncio.StreamReaderProtocol(reader)
        proto.aioopenssl_test_reader = reader
        return proto

    async def _connect(self, *args, **kwargs):
        transport, reader_proto = \
            await aioopenssl.create_starttls_connection(
                asyncio.get_event_loop(),
                self._stream_reader_proto,
                *args,
                **kwargs
            )
        reader = reader_proto.aioopenssl_test_reader
        del reader_proto.aioopenssl_test_reader
        writer = asyncio.StreamWriter(transport, reader_proto, reader,
                                      self.loop)
        return transport, reader, writer

    @blocking
    async def test_connect_send_recv_close(self):
        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT+1,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=False,
        )

        sock = await self._get_inbound()

        c_writer.write(b"foobar")
        await self.send_thread(sock, b"fnord")

        await asyncio.gather(c_writer.drain())

        c_read, s_read = await asyncio.gather(
            c_reader.readexactly(5),
            self.recv_thread(sock, 6)
        )

        self.assertEqual(
            s_read,
            b"foobar"
        )

        self.assertEqual(
            c_read,
            b"fnord"
        )

        c_transport.close()
        await asyncio.sleep(0.1)
        sock.close()

    @blocking
    async def test_renegotiate(self):
        ctx = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)
        if hasattr(OpenSSL.SSL, "OP_NO_TLSv1_3"):
            # Need to forbid TLS v1.3, since TLSv1.3+ does not support
            # renegotiation
            ctx.set_options(OpenSSL.SSL.OP_NO_TLSv1_3)
        ctx.use_certificate_chain_file(str(KEYFILE))
        ctx.use_privatekey_file(str(KEYFILE))
        self._replace_thread(ctx)

        c_transport, c_reader, c_writer = await self._connect(
            host="127.0.0.1",
            port=PORT+1,
            ssl_context_factory=lambda transport: OpenSSL.SSL.Context(
                OpenSSL.SSL.SSLv23_METHOD
            ),
            server_hostname="localhost",
            use_starttls=False,
        )

        sock = await self._get_inbound()

        c_writer.write(b"foobar")
        await self.send_thread(sock, b"fnord")

        await asyncio.gather(c_writer.drain())

        c_read, s_read = await asyncio.gather(
            c_reader.readexactly(5),
            self.recv_thread(sock, 6)
        )

        self.assertEqual(
            s_read,
            b"foobar"
        )

        self.assertEqual(
            c_read,
            b"fnord"
        )

        try:
            sock.renegotiate()
        except OpenSSL.SSL.Error as exc:
            (argv,), = exc.args
            if (argv[1] == "SSL_renegotiate" and
                    argv[2] == "wrong ssl version"):
                raise RuntimeError(
                    "You are a PyOpenSSL version which uses TLSv1.3, but has"
                    " no way to turn it off. Update PyOpenSSL."
                )
            raise

        c_writer.write(b"baz")

        await asyncio.gather(
            c_writer.drain(),
            self.loop.run_in_executor(None, sock.do_handshake)
        )

        s_read, = await asyncio.gather(
            self.recv_thread(sock, 6)
        )

        self.assertEqual(s_read, b"baz")

        c_transport.close()
        await asyncio.sleep(0.1)
        sock.close()
