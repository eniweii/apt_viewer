import struct
def load_lang(fn):
    d=open(fn,'rb').read()
    magic,size,n,t0,sbase=struct.unpack('<5I',d[:20])
    ents=[struct.unpack('<II',d[t0+8*i:t0+8*i+8]) for i in range(n)]
    out={}
    for h,o in ents:
        if h==0 and o==0: continue
        e=d.index(b'\0',sbase+o); out[h]=d[sbase+o:e]
    return out
def lookup2(data, initval):
    M=0xffffffff
    def mix(a,b,c):
        a=(a-b-c)&M; a^=c>>13; b=(b-c-a)&M; b^=(a<<8)&M; c=(c-a-b)&M; c^=b>>13
        a=(a-b-c)&M; a^=c>>12; b=(b-c-a)&M; b^=(a<<16)&M; c=(c-a-b)&M; c^=b>>5
        a=(a-b-c)&M; a^=c>>3;  b=(b-c-a)&M; b^=(a<<10)&M; c=(c-a-b)&M; c^=b>>15
        return a,b,c
    a=b=0x9e3779b9; c=initval; l=len(data); k=data; i=0
    while l-i>=12:
        a=(a+struct.unpack('<I',k[i:i+4])[0])&M; b=(b+struct.unpack('<I',k[i+4:i+8])[0])&M; c=(c+struct.unpack('<I',k[i+8:i+12])[0])&M
        a,b,c=mix(a,b,c); i+=12
    c=(c+len(data))&M; rem=l-i; t=k[i:]
    if rem>=11: c=(c+(t[10]<<24))&M
    if rem>=10: c=(c+(t[9]<<16))&M
    if rem>=9: c=(c+(t[8]<<8))&M
    if rem>=8: b=(b+(t[7]<<24))&M
    if rem>=7: b=(b+(t[6]<<16))&M
    if rem>=6: b=(b+(t[5]<<8))&M
    if rem>=5: b=(b+t[4])&M
    if rem>=4: a=(a+(t[3]<<24))&M
    if rem>=3: a=(a+(t[2]<<16))&M
    if rem>=2: a=(a+(t[1]<<8))&M
    if rem>=1: a=(a+t[0])&M
    a,b,c=mix(a,b,c); return c
