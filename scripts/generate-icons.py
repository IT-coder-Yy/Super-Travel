"""Generate the app's original geometric compass icon with the Python standard library."""
from pathlib import Path
import math
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1] / "src-tauri" / "icons"

def triangle(x, y, a, b, c):
    def sign(p, q): return (x-q[0])*(p[1]-q[1])-(p[0]-q[0])*(y-q[1])
    d = [sign(a,b),sign(b,c),sign(c,a)]
    return not (any(v < 0 for v in d) and any(v > 0 for v in d))

def png(size):
    pixels = bytearray()
    for row in range(size):
        pixels.append(0)
        for col in range(size):
            x, y = (col+.5)/size*2-1, (row+.5)/size*2-1
            radius = math.hypot(x, y)
            color = (247, 251, 255, 255)
            if max(abs(x),abs(y)) > .70 and math.hypot(max(abs(x)-.70,0),max(abs(y)-.70,0)) > .27: color = (0,0,0,0)
            elif .65 < radius < .70: color = (22,43,77,255)
            elif triangle(x,y,(.22,-.57),(-.24,.18),(.02,.04)): color = (16,81,202,255)
            elif triangle(x,y,(-.22,.57),(.24,-.18),(-.02,-.04)): color = (22,43,77,255)
            elif radius < .065: color = (22,43,77,255)
            pixels.extend(color)
    def chunk(name, data): return struct.pack('>I',len(data))+name+data+struct.pack('>I',zlib.crc32(name+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',size,size,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(bytes(pixels),9))+chunk(b'IEND',b'')

def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    images={size:png(size) for size in [16,32,48,128,256,512]}
    for size in [32,128]: (ROOT/f'{size}x{size}.png').write_bytes(images[size])
    sizes=[16,32,48,256]
    offset=6+16*len(sizes)
    entries=[]
    for size in sizes:
        data=images[size]
        entries.append(struct.pack('<BBBBHHII',size%256,size%256,0,0,1,32,len(data),offset))
        offset+=len(data)
    (ROOT/'icon.ico').write_bytes(struct.pack('<HHH',0,1,len(sizes))+b''.join(entries)+b''.join(images[s] for s in sizes))
    blocks=b''.join(code+struct.pack('>I',8+len(images[s]))+images[s] for code,s in [(b'ic07',128),(b'ic08',256),(b'ic09',512)])
    (ROOT/'icon.icns').write_bytes(b'icns'+struct.pack('>I',8+len(blocks))+blocks)

if __name__=='__main__': main()
