import pefile, struct, re, json, collections
pe=pefile.PE('/tmp/stuff/nfs.exe'); base=0x400000
data=open('/tmp/stuff/nfs.exe','rb').read()
tx=pe.sections[0]; t0=tx.PointerToRawData; tv=base+tx.VirtualAddress; tn=tx.Misc_VirtualSize
text=data[t0:t0+tn]
def va_of(o):
    for s in pe.sections:
        if s.PointerToRawData<=o<s.PointerToRawData+s.SizeOfRawData: return base+s.VirtualAddress+o-s.PointerToRawData
def find_str(name):
    return [m.start() for m in re.finditer(re.escape(name.encode())+b'\0',data) if data[m.start()-1:m.start()]==b'\0']
def fstart(pos):
    fs=data.rfind(b'\xcc\xcc',t0,pos); return tv+(fs+2-t0)
def sites(name):
    out=[]
    for o in find_str(name):
        sv=va_of(o)
        for m in re.finditer(re.escape(b'\x68'+struct.pack('<I',sv)),text):
            p=t0+m.start(); seg=data[p+5:p+40]; c=seg.find(b'\xe8'); tgt=None
            if c>=0:
                rel=struct.unpack('<i',seg[c+1:c+5])[0]; tgt=tv+(p+5+c-t0)+5+rel
            out.append((tv+m.start(),fstart(p),tgt))
    return out
