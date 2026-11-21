"""Reading what a client sends through a tunnel. A client that asks the proxy to CONNECT to a host then speaks to
that host through it: usually TLS, which is ended here with a certificate of the installation's authority, and
sometimes plain HTTP, which some clients send through a tunnel all the same. The first byte tells them apart."""

import asyncio
import ssl

from harnessgateway.proxy.authority import Authority
from harnessgateway.proxy.http import HEAD_MAX

_TLS_HANDSHAKE = b"\x16"
_GONE = (ConnectionError, ssl.SSLError, OSError)


class Inside:
    """The inside of a tunnel: `reader` gives what the client sends, in the clear, and `write` answers it."""

    def __init__(self, raw_reader: asyncio.StreamReader, raw_writer: asyncio.StreamWriter,
                 tls: ssl.SSLObject | None, arriving: ssl.MemoryBIO, leaving: ssl.MemoryBIO) -> None:
        self._raw_reader, self._raw_writer = raw_reader, raw_writer
        self._tls, self._arriving, self._leaving = tls, arriving, leaving
        self.reader = asyncio.StreamReader(limit=HEAD_MAX)
        self._reading: asyncio.Task[None] | None = None

    @property
    def encrypted(self) -> bool:
        return self._tls is not None

    @classmethod
    async def open(cls, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, authority: Authority,
                   host: str) -> "Inside | None":
        """The inside of the tunnel a client has just been granted; None when it went away first."""
        first = await reader.read(65536)
        if not first:
            return None
        arriving, leaving = ssl.MemoryBIO(), ssl.MemoryBIO()
        if first[:1] != _TLS_HANDSHAKE:
            inside = cls(reader, writer, None, arriving, leaving)
            inside.reader.feed_data(first)
        else:
            tls = authority.context_for(host).wrap_bio(arriving, leaving, server_side=True)
            inside = cls(reader, writer, tls, arriving, leaving)
            arriving.write(first)
            while True:
                try:
                    tls.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    await inside.drain()
                    more = await reader.read(65536)
                    if not more:
                        return None
                    arriving.write(more)
            await inside.drain()
        inside._reading = asyncio.create_task(inside._read())
        return inside

    async def _read(self) -> None:
        """Move what the client sends into `reader`, decrypted when it is TLS."""
        try:
            while True:
                if self._tls is not None:
                    try:
                        clear = self._tls.read(65536)
                    except ssl.SSLWantReadError:
                        clear = None
                    if clear:
                        self.reader.feed_data(clear)
                        continue
                    if clear == b"":                            # the client ended the TLS session
                        break
                raw = await self._raw_reader.read(65536)
                if not raw:
                    break
                if self._tls is not None:
                    self._arriving.write(raw)
                else:
                    self.reader.feed_data(raw)
        except _GONE:
            pass
        finally:
            self.reader.feed_eof()

    def write(self, data: bytes) -> None:
        if self._tls is not None:
            self._tls.write(data)
            data = self._leaving.read()
        self._raw_writer.write(data)

    async def drain(self) -> None:
        if pending := self._leaving.read():                     # what a handshake left to send
            self._raw_writer.write(pending)
        await self._raw_writer.drain()

    def close(self) -> None:
        if self._reading is not None:
            self._reading.cancel()
