"""Small local pipe format. This is not a network camera protocol."""
import struct

HEADER = struct.Struct("!4sIdHHHHBQ")
MAGIC = b"GVC1"
MAX_BYTES = 32 * 1024 * 1024
JPEG, BGR = 0, 1


def send(stream, data, timestamp, width=0, height=0, original_width=0,
         original_height=0, encoding=JPEG, lost=0):
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("invalid local frame size")
    stream.write(HEADER.pack(MAGIC, len(data), timestamp, width, height,
                             original_width, original_height, encoding, lost))
    stream.write(data)
    stream.flush()


def read_exact(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        data = stream.read(count - len(chunks))
        if not data:
            raise EOFError("camera worker pipe closed")
        chunks.extend(data)
    return bytes(chunks)


def receive(stream):
    magic, size, stamp, width, height, ow, oh, encoding, lost = HEADER.unpack(read_exact(stream, HEADER.size))
    if magic != MAGIC or not 0 < size <= MAX_BYTES or encoding not in (JPEG, BGR):
        raise ValueError("invalid local camera frame header")
    if encoding == BGR and (not width or not height or size != width * height * 3):
        raise ValueError("invalid BGR dimensions")
    return read_exact(stream, size), stamp, width, height, ow, oh, encoding, lost
