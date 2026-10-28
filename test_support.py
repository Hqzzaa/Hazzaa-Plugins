import struct


def plugin_bytes(payload=b'fixture', machine=0x8664):
    if isinstance(payload, str):
        payload = payload.encode()
    data = bytearray(1024)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 132, machine, 1, 0, 0, 0, 240, 0x2022)
    optional = 152
    struct.pack_into('<H', data, optional, 0x20b)
    struct.pack_into('<Q', data, optional+24, 0x140000000)
    struct.pack_into('<II', data, optional+32, 0x1000, 0x200)
    struct.pack_into('<II', data, optional+56, 0x2000, 0x200)
    struct.pack_into('<H', data, optional+68, 3)
    struct.pack_into('<I', data, optional+108, 16)
    struct.pack_into('<II', data, optional+112+16, 0x1000, 0x200)
    section = optional+240
    data[section:section+8] = b'.rsrc\0\0\0'
    struct.pack_into('<IIII', data, section+8, 0x200, 0x1000, 0x200, 0x200)
    struct.pack_into('<I', data, section+36, 0x40000040)
    root = 512
    struct.pack_into('<HH', data, root+12, 1, 0)
    struct.pack_into('<II', data, root+16, 0x80000080, 0x80000030)
    struct.pack_into('<HH', data, root+0x30+12, 0, 1)
    struct.pack_into('<II', data, root+0x40, 16000, 0x80000050)
    struct.pack_into('<HH', data, root+0x50+12, 0, 1)
    struct.pack_into('<II', data, root+0x60, 1033, 0x70)
    struct.pack_into('<IIII', data, root+0x70, 0x10a0, 8, 0, 0)
    struct.pack_into('<H', data, root+0x80, 4)
    data[root+0x82:root+0x8a] = 'PiPL'.encode('utf-16le')
    return bytes(data)+payload
